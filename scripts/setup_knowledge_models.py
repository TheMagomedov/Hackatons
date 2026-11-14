"""Explicit one-time model download. Documents are never sent by this script."""

import hashlib
import os
from pathlib import Path

import httpx

HASHES = {
    'rus': 'e16e5e036cce1d9ec2b00063cf8b54472625b9e14d893a169e2b0dedeb4df225',
    'eng': '7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2',
}


def main():
    root = Path(__file__).resolve().parents[1] / 'var' / 'models'
    tessdata = root / 'tessdata'
    tessdata.mkdir(parents=True, exist_ok=True)
    for language, digest in HASHES.items():
        target = tessdata / (language+'.traineddata')
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            continue
        response = httpx.get('https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/'+target.name,
                             timeout=60)
        response.raise_for_status()
        if hashlib.sha256(response.content).hexdigest() != digest:
            raise RuntimeError('OCR model checksum changed; review the upstream model before updating.')
        target.write_bytes(response.content)
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    from fastembed import TextEmbedding

    model = TextEmbedding(model_name='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',
        cache_dir=str(root / 'embeddings'), providers=['CPUExecutionProvider'], cuda=False, threads=2)
    assert len(next(model.embed(['Проверка локального поиска']))) == 384
    print('Local OCR and multilingual embedding models are ready.')
    print('OCR_TESSDATA_DIR=' + str(tessdata))
    print('RAG_EMBEDDINGS_DIR=' + str(root / 'embeddings'))


if __name__ == '__main__':
    main()
