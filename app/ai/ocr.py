"""Russian/English OCR in the existing killable PDF worker; no cloud traffic."""

import os
from pathlib import Path

from app.shared.errors import InvalidInput, ProviderUnavailable


def ocr_pages(data: bytes, pages: list[int], max_chars: int) -> dict[int, str]:
    location = os.environ.get('OCR_TESSDATA_DIR', '').strip()
    if not location:
        raise InvalidInput('В PDF нет текстового слоя. OCR не настроен; пришлите текстовый PDF.')
    folder = Path(location)
    if not all((folder / (language+'.traineddata')).is_file() for language in ('rus', 'eng')):
        raise ProviderUnavailable('Не установлены локальные языковые данные OCR (rus и eng).')
    if len(pages) > 6:
        raise InvalidInput('За один раз распознаётся не больше 6 сканированных страниц. Разделите PDF.')
    try:
        import pypdfium2 as pdfium
        from tesserocr import PyTessBaseAPI
    except ImportError:
        raise ProviderUnavailable('Установите локальные зависимости OCR из requirements-knowledge.txt.') from None
    result, count = {}, 0
    with pdfium.PdfDocument(data) as pdf, PyTessBaseAPI(path=str(folder), lang='rus+eng') as engine:
        for index in pages:
            page = pdf[index]
            try:
                width, height = page.get_size()
                if min(width, height) <= 0 or max(width, height) > 15_000:
                    raise InvalidInput('Слишком большой размер страницы для OCR.')
                scale = min(2.5, 3200 / max(width, height))
                bitmap = page.render(scale=scale)
                try:
                    with bitmap.to_pil() as image:
                        engine.SetImage(image)
                        text = engine.GetUTF8Text().strip()
                        count += len(text)
                        if count > max_chars:
                            raise InvalidInput('В скане слишком много текста. Разделите документ.')
                        result[index] = text
                finally:
                    bitmap.close()
            finally:
                page.close()
    return result
