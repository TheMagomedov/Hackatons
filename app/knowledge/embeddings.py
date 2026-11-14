"""CPU multilingual embeddings from preinstalled local files only."""

import math
import os
import threading
from pathlib import Path

from app.shared.errors import ProviderUnavailable

MODEL = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'


class LocalEmbeddings:
    model_id = MODEL + ':fastembed-onnx-v1'

    def __init__(self, cache_dir: str | Path):
        self.cache_dir = str(Path(cache_dir).resolve())
        self._model = None
        self._lock = threading.Lock()

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            try:
                if self._model is None:
                    os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
                    from fastembed import TextEmbedding

                    self._model = TextEmbedding(model_name=MODEL, cache_dir=self.cache_dir,
                        local_files_only=True, providers=['CPUExecutionProvider'], cuda=False, threads=2)
                values = []
                for vector in self._model.embed(texts, batch_size=8):
                    numbers = [float(v) for v in vector]
                    if len(numbers) != 384 or not all(math.isfinite(v) for v in numbers):
                        raise ValueError
                    norm = math.sqrt(sum(v*v for v in numbers))
                    if norm <= 0:
                        raise ValueError
                    values.append([v/norm for v in numbers])
                if len(values) != len(texts):
                    raise ValueError
                return values
            except Exception:
                raise ProviderUnavailable('Локальная модель поиска недоступна. Проверьте установку моделей знаний.') from None
