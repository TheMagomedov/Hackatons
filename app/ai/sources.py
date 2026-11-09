import asyncio
import io
import ipaddress
import re
import socket
from html.parser import HTMLParser

import aiohttp
from aiohttp.abc import AbstractResolver
from pypdf import PdfReader
from yarl import URL

from app.shared.models import ExtractedSource, Source
from app.shared.ports import AppError

MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_HTML_BYTES = 1024 * 1024
MAX_TEXT = 16_000
MAX_PAGES = 24


def public_ip(value: str) -> bool:
    addr = ipaddress.ip_address(value)
    return (
        addr.is_global and not addr.is_multicast and not addr.is_reserved
        and not (addr.version == 6 and (
            addr.ipv4_mapped or addr.sixtofour or addr.teredo
            or addr in ipaddress.ip_network("64:ff9b::/96")
        ))
    )


class PublicResolver(AbstractResolver):
    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC):
        records = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM
        )
        # Pass only the addresses inspected here to the connector.
        if not records or any(not public_ip(addr[0]) for _, _, _, _, addr in records):
            raise OSError("Non-public destination")
        return [
            {"hostname": host, "host": addr[0], "port": addr[1],
             "family": af, "proto": proto, "flags": socket.AI_NUMERICHOST}
            for af, _, proto, _, addr in records
        ]

    async def close(self) -> None:
        pass


class PageText(HTMLParser):
    hidden = {"script", "style", "noscript", "template", "svg", "nav", "header", "footer"}
    breaks = {"p", "br", "h1", "h2", "h3", "h4", "div", "li", "section", "article", "tr"}

    def __init__(self):
        super().__init__()
        self.skipping: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden:
            self.skipping.append(tag)
        if not self.skipping and tag in self.breaks:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.skipping and self.skipping[-1] == tag:
            self.skipping.pop()
        if not self.skipping and tag in self.breaks:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)

    def extract(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)[:MAX_TEXT]


async def read_website(value: str) -> str:
    try:
        url = URL(value)
        if (
            url.scheme not in {"http", "https"} or not url.host or url.user is not None
            or url.port != (443 if url.scheme == "https" else 80)
            or "%" in url.host
        ):
            raise ValueError
        try:
            literal_ip = ipaddress.ip_address(url.host)
        except ValueError:
            literal_ip = None
        if literal_ip is not None and not public_ip(url.host):
            raise ValueError
    except (ValueError, UnicodeError) as exc:
        raise AppError("Укажите публичный HTTP(S)-адрес страницы.") from exc

    connector = aiohttp.TCPConnector(resolver=PublicResolver())
    timeout = aiohttp.ClientTimeout(total=12)
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(url, allow_redirects=False) as response:
                if 300 <= response.status < 400:
                    raise AppError("Страница перенаправляет запрос. Пришлите её конечный адрес.")
                if response.status != 200 or response.content_type not in {"text/html", "application/xhtml+xml"}:
                    raise AppError("По адресу не удалось получить HTML-страницу.")
                data = bytearray()
                async for chunk in response.content.iter_chunked(16_384):
                    data.extend(chunk)
                    if len(data) > MAX_HTML_BYTES:
                        raise AppError("Страница превышает лимит 1 МиБ.")
                try:
                    html = data.decode(response.charset or "utf-8", errors="replace")
                except LookupError:
                    html = data.decode("utf-8", errors="replace")
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise AppError("Не удалось загрузить публичную страницу. Проверьте адрес.") from exc
    parser = PageText()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, AssertionError) as exc:
        raise AppError("Разметку страницы не удалось разобрать.") from exc
    if not (text := parser.extract()):
        raise AppError("На странице не найден текст.")
    return text


def read_pdf(data: bytes) -> str:
    if not data.startswith(b"%PDF-") or len(data) > MAX_PDF_BYTES:
        raise AppError("Нужен PDF-файл размером до 8 МиБ.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise AppError("Пришлите PDF без пароля.")
        if len(reader.pages) > MAX_PAGES:
            raise AppError("В PDF должно быть не больше 24 страниц.")
        parts = []
        remaining = MAX_TEXT
        for page in reader.pages:
            part = re.sub(r"[ \t]+", " ", page.extract_text()).strip()[:remaining]
            parts.append(part)
            remaining -= len(part) + 1
            if remaining <= 0:
                break
    except AppError:
        raise
    except Exception as exc:
        raise AppError("PDF повреждён или его текстовый слой не удалось прочитать.") from exc
    text = "\n".join(part for part in parts if part)[:MAX_TEXT]
    if not text:
        raise AppError("В PDF нет текстового слоя. Пришлите текстовый PDF.")
    return text


async def extract_source(source: Source, content: str | bytes) -> ExtractedSource:
    if source.kind == "website" and isinstance(content, str):
        text = await read_website(content)
    elif source.kind == "pdf" and isinstance(content, bytes):
        text = await asyncio.to_thread(read_pdf, content)
    elif source.kind == "notes" and isinstance(content, str) and content.strip():
        text = content.strip()[:MAX_TEXT]
    else:
        raise AppError("Проверьте формат источника.")
    return ExtractedSource(source=source, text=text)
