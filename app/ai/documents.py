"""Local bounded DOCX extraction. Never runs macros or fetches linked resources."""

import io
import zipfile
from xml.etree import ElementTree

from app.shared.errors import InvalidInput

DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
WORD = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def extract_docx_text(data: bytes, *, max_chars: int = 20_000) -> str:
    if not data or len(data) > 8 * 1024 * 1024:
        raise InvalidInput('Документ Word должен быть не больше 8 МБ.')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if (len(entries) > 2000 or len({i.filename for i in entries}) != len(entries)
                    or sum(i.file_size for i in entries) > 32 * 1024 * 1024
                    or any(i.flag_bits & 1 for i in entries)):
                raise ValueError
            names = {item.filename for item in entries}
            if '[Content_Types].xml' not in names or 'word/document.xml' not in names:
                raise ValueError
            if any('vbaproject' in name.lower() for name in names):
                raise InvalidInput('Документы с макросами не поддерживаются.')
            # Only document text and tables. Relationships, hyperlinks, embedded
            # objects, scripts and external references are never evaluated.
            info = archive.getinfo('word/document.xml')
            if info.file_size > 4 * 1024 * 1024:
                raise ValueError
            xml = archive.read(info)
            # Reject declarations rather than allowing XML entity expansion.
            # Word XML is UTF-8/UTF-16; stripping NUL catches both encodings.
            marker = xml.replace(b'\x00', b'').upper()
            if b'<!DOCTYPE' in marker or b'<!ENTITY' in marker:
                raise ValueError
            root = ElementTree.fromstring(xml)
            parts = []
            count = 0
            for paragraph in root.iter(WORD + 'p'):
                text = ''.join(node.text or '' for node in paragraph.iter(WORD + 't')).strip()
                count += len(text) + 1
                if count > max_chars + 1:
                    raise InvalidInput('В документе слишком много текста. Разделите его на части.')
                if text:
                    parts.append(text)
            if not parts:
                raise InvalidInput('В документе Word нет текста. Изображения и сканы не извлекаются.')
            return '\n'.join(parts)
    except InvalidInput:
        raise
    except (ValueError, KeyError, OSError, RuntimeError, zipfile.BadZipFile, ElementTree.ParseError):
        raise InvalidInput('Не удалось прочитать DOCX. Пришлите документ Word без пароля.') from None
