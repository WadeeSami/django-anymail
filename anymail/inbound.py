from base64 import b64decode
from email.message import Message
from email.utils import unquote

import six
from django.core.files.uploadedfile import SimpleUploadedFile

from ._email_compat import EmailParser, EmailBytesParser
from .utils import angle_wrap, get_content_disposition, parse_address_list, parse_rfc2822date


class AnymailInboundMessage(Message, object):  # `object` ensures new-style class in Python 2)
    """
    A normalized, parsed inbound email message.

    A subclass of email.message.Message, with some additional
    convenience properties, plus helpful methods backported
    from Python 3.6+ email.message.EmailMessage (or really, MIMEPart)
    """

    # Why Python email.message.Message rather than django.core.mail.EmailMessage?
    # Django's EmailMessage is really intended for constructing a (limited subset of)
    # Message to send; Message is better designed for representing arbitrary messages:
    #
    # * Message is easily parsed from raw mime (which is an inbound format provided
    #   by many ESPs), and can accurately represent any mime email that might be received
    # * Message can represent repeated header fields (e.g., "Received") which
    #   are common in inbound messages
    # * Django's EmailMessage defaults a bunch of properties in ways that aren't helpful
    #   (e.g., from_email from settings)

    def __init__(self, *args, **kwargs):
        # Note: this must accept zero arguments, for use with message_from_string (email.parser)
        super(AnymailInboundMessage, self).__init__(*args, **kwargs)

        # Additional attrs provided by some ESPs:
        self.envelope_sender = None
        self.envelope_recipient = None
        self.stripped_text = None
        self.stripped_html = None
        self.spam_detected = None
        self.spam_score = None

    #
    # Convenience accessors
    #

    @property
    def from_email(self):
        """EmailAddress """
        # equivalent to Python 3.2+ message['From'].addresses[0]
        from_email = self.get_address_header('From')
        if len(from_email) == 1:
            return from_email[0]
        elif len(from_email) == 0:
            return None
        else:
            return from_email  # unusual, but technically-legal multiple-From; preserve list

    @property
    def to(self):
        """list of EmailAddress objects from To header"""
        # equivalent to Python 3.2+ message['To'].addresses
        return self.get_address_header('To')

    @property
    def cc(self):
        """list of EmailAddress objects from Cc header"""
        # equivalent to Python 3.2+ message['Cc'].addresses
        return self.get_address_header('Cc')

    @property
    def subject(self):
        """str value of Subject header, or None"""
        return self['Subject']

    @property
    def date(self):
        """datetime.datetime from Date header, or None if missing/invalid"""
        # equivalent to Python 3.2+ message['Date'].datetime
        return self.get_date_header('Date')

    @property
    def text(self):
        """Contents of the (first) text/plain body part, or None"""
        return self._get_body_content('text/plain')

    @property
    def html(self):
        """Contents of the (first) text/html body part, or None"""
        return self._get_body_content('text/html')

    @property
    def attachments(self):
        """list of attachments (as MIMEPart objects); excludes inlines"""
        return [part for part in self.walk() if part.is_attachment()]

    @property
    def inline_attachments(self):
        """dict of Content-ID: attachment (as MIMEPart objects)"""
        return {unquote(part['Content-ID']): part for part in self.walk()
                if part.is_inline_attachment() and part['Content-ID']}

    def get_address_header(self, header):
        """Return the value of header parsed into a (possibly-empty) list of EmailAddress objects"""
        values = self.get_all(header)
        if values is not None:
            values = parse_address_list(values)
        return values or []

    def get_date_header(self, header):
        """Return the value of header parsed into a datetime.date, or None"""
        value = self[header]
        if value is not None:
            value = parse_rfc2822date(value)
        return value

    def _get_body_content(self, content_type):
        # This doesn't handle as many corner cases as Python 3.6 email.message.EmailMessage.get_body,
        # but should work correctly for nearly all real-world inbound messages.
        # We're guaranteed to have `is_attachment` available, because all AnymailInboundMessage parts
        # should themselves be AnymailInboundMessage.
        for part in self.walk():
            if part.get_content_type() == content_type and not part.is_attachment():
                return part.get_content_text()
        return None

    # Backport from Python 3.5 email.message.Message
    def get_content_disposition(self):
        try:
            return super(AnymailInboundMessage, self).get_content_disposition()
        except AttributeError:
            return get_content_disposition(self)

    # Backport from Python 3.4.2 email.message.MIMEPart
    def is_attachment(self):
        return self.get_content_disposition() == 'attachment'

    # New for Anymail
    def is_inline_attachment(self):
        return self.get_content_disposition() == 'inline'

    def get_content_bytes(self):
        """Return the raw payload bytes"""
        maintype = self.get_content_maintype()
        if maintype == 'message':
            # The attachment's payload is a single (parsed) email Message; flatten it to bytes.
            # (Note that self.is_multipart() misleadingly returns True in this case.)
            payload = self.get_payload()
            assert len(payload) == 1  # should be exactly one message
            try:
                return payload[0].as_bytes()  # Python 3
            except AttributeError:
                return payload[0].as_string().encode('utf-8')
        elif maintype == 'multipart':
            # The attachment itself is multipart; the payload is a list of parts,
            # and it's not clear which one is the "content".
            raise ValueError("get_content_bytes() is not valid on multipart messages "
                             "(perhaps you want as_bytes()?)")
        return self.get_payload(decode=True)

    def get_content_text(self, charset=None, errors=None):
        """Return the payload decoded to text"""
        maintype = self.get_content_maintype()
        if maintype == 'message':
            # The attachment's payload is a single (parsed) email Message; flatten it to text.
            # (Note that self.is_multipart() misleadingly returns True in this case.)
            payload = self.get_payload()
            assert len(payload) == 1  # should be exactly one message
            return payload[0].as_string()
        elif maintype == 'multipart':
            # The attachment itself is multipart; the payload is a list of parts,
            # and it's not clear which one is the "content".
            raise ValueError("get_content_text() is not valid on multipart messages "
                             "(perhaps you want as_string()?)")
        else:
            payload = self.get_payload(decode=True)
            if payload is None:
                return payload
            charset = charset or self.get_content_charset('US-ASCII')
            errors = errors or 'replace'
            return payload.decode(charset, errors=errors)

    def as_uploaded_file(self):
        """Return the attachment converted to a Django UploadedFile"""
        if self['Content-Disposition'] is None:
            return None  # this part is not an attachment
        name = self.get_filename()
        content_type = self.get_content_type()
        content = self.get_content_bytes()
        return SimpleUploadedFile(name, content, content_type)

    #
    # Construction
    #
    # These methods are intended primarily for internal Anymail use
    # (in inbound webhook handlers)

    @classmethod
    def parse_raw_mime(cls, s):
        """Returns a new AnymailInboundMessage parsed from str s"""
        if isinstance(s, six.text_type):
            # Avoid Python 3.x issue https://bugs.python.org/issue18271
            # (See test_inbound: test_parse_raw_mime_8bit_utf8)
            return cls.parse_raw_mime_bytes(s.encode('utf-8'))
        return EmailParser(cls).parsestr(s)

    @classmethod
    def parse_raw_mime_bytes(cls, b):
        """Returns a new AnymailInboundMessage parsed from bytes b"""
        return EmailBytesParser(cls).parsebytes(b)

    @classmethod
    def parse_raw_mime_file(cls, fp):
        """Returns a new AnymailInboundMessage parsed from file-like object fp"""
        if isinstance(fp.read(0), six.binary_type):
            return EmailBytesParser(cls).parse(fp)
        else:
            return EmailParser(cls).parse(fp)

    @classmethod
    def construct(cls, raw_headers=None, from_email=None, to=None, cc=None, subject=None, headers=None,
                  text=None, text_charset='utf-8', html=None, html_charset='utf-8',
                  attachments=None):
        """
        Returns a new AnymailInboundMessage constructed from params.

        This is designed to handle the sorts of email fields typically present
        in ESP parsed inbound messages. (It's not a generalized MIME message constructor.)

        :param raw_headers: {str|None} base (or complete) message headers as a single string
        :param from_email: {str|None} value for From header
        :param to: {str|None} value for To header
        :param cc: {str|None} value for Cc header
        :param subject: {str|None} value for Subject header
        :param headers: {sequence[(str, str)]|mapping|None} additional headers
        :param text: {str|None} plaintext body
        :param text_charset: {str} charset of plaintext body; default utf-8
        :param html: {str|None} html body
        :param html_charset: {str} charset of html body; default utf-8
        :param attachments: {list[MIMEBase]|None} as returned by construct_attachment
        :return: {AnymailInboundMessage}
        """
        if raw_headers is not None:
            msg = EmailParser(cls).parsestr(raw_headers, headersonly=True)
            msg.set_payload(None)  # headersonly forces an empty string payload, which breaks things later
        else:
            msg = cls()

        if from_email is not None:
            del msg['From']  # override raw_headers value, if any
            msg['From'] = from_email
        if to is not None:
            del msg['To']
            msg['To'] = to
        if cc is not None:
            del msg['Cc']
            msg['Cc'] = cc
        if subject is not None:
            del msg['Subject']
            msg['Subject'] = subject
        if headers is not None:
            try:
                header_items = headers.items()  # mapping
            except AttributeError:
                header_items = headers  # sequence of (key, value)
            for name, value in header_items:
                msg.add_header(name, value)

        # For simplicity, we always build a MIME structure that could support plaintext/html
        # alternative bodies, inline attachments for the body(ies), and message attachments.
        # This may be overkill for simpler messages, but the structure is never incorrect.
        del msg['MIME-Version']  # override raw_headers values, if any
        del msg['Content-Type']
        msg['MIME-Version'] = '1.0'
        msg['Content-Type'] = 'multipart/mixed'

        related = cls()  # container for alternative bodies and inline attachments
        related['Content-Type'] = 'multipart/related'
        msg.attach(related)

        alternatives = cls()  # container for text and html bodies
        alternatives['Content-Type'] = 'multipart/alternative'
        related.attach(alternatives)

        if text is not None:
            part = cls()
            part['Content-Type'] = 'text/plain'
            part.set_payload(text, charset=text_charset)
            alternatives.attach(part)
        if html is not None:
            part = cls()
            part['Content-Type'] = 'text/html'
            part.set_payload(html, charset=html_charset)
            alternatives.attach(part)

        if attachments is not None:
            for attachment in attachments:
                if attachment.is_inline_attachment():
                    related.attach(attachment)
                else:
                    msg.attach(attachment)

        return msg

    @classmethod
    def construct_attachment_from_uploaded_file(cls, file, content_id=None):
        # This pulls the entire file into memory; it would be better to implement
        # some sort of lazy attachment where the content is only pulled in if/when
        # requested (and then use file.chunks() to minimize memory usage)
        return cls.construct_attachment(
            content_type=file.content_type,
            content=file.read(),
            filename=file.name,
            content_id=content_id,
            charset=file.charset)

    @classmethod
    def construct_attachment(cls, content_type, content,
                             charset=None, filename=None, content_id=None, base64=False):
        part = cls()
        part['Content-Type'] = content_type
        part['Content-Disposition'] = 'inline' if content_id is not None else 'attachment'

        if filename is not None:
            part.set_param('name', filename, header='Content-Type')
            part.set_param('filename', filename, header='Content-Disposition')

        if content_id is not None:
            part['Content-ID'] = angle_wrap(content_id)

        if base64:
            content = b64decode(content)

        payload = content
        if part.get_content_maintype() == 'message':
            # email.Message parses message/rfc822 parts as a "multipart" (list) payload
            # whose single item is the recursively-parsed message attachment
            if isinstance(content, six.binary_type):
                content = content.decode()
            payload = [cls.parse_raw_mime(content)]
            charset = None

        part.set_payload(payload, charset)
        return part
