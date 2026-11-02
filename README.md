# Qwen serving on vLLM

Локальный API для Qwen2.5-1.5B-Instruct через vLLM 0.27.0 на CPU.
Контейнер работает от пользователя `app` с UID/GID 10001, хранит файлы модели
в Docker volume и требует Bearer token для запросов к `/v1/*`.

Этот этап проекта покрывает Docker. Kubernetes, PVC, Secret и ConfigMap будут
добавлены отдельно; Docker volume и `.env` используются для локальной проверки.

## Требования

- Linux x86_64 и работающий Docker Engine.
- CPU с AVX2 или AVX512. AVX2 имеет ограниченную поддержку в vLLM;
  фактическая проверка выполнена пользователем на Ryzen 5 4500U до этих доработок.
- 16 ГБ RAM как практический ориентир для небольшого контекста.
  В `float32` только веса занимают около 6 ГБ RAM, дополнительно нужны
  память движка и 1 GiB KV-кеша. Закройте тяжёлые приложения перед запуском.
- Около 7.5 ГБ для образа, 3.1 ГБ для файлов модели и запас для Docker-кеша.
- Интернет при первой загрузке образа и модели. Проверка готовой модели
  выполняется локально, без Hub API.

## Состав проекта

- `Dockerfile` — CPU-образ, пользователь без root, проверка lock и healthcheck.
- `requirements.txt` — полный список точных версий Python-пакетов базового образа.
- `config/model-manifest.json` — репозиторий, commit модели, размеры и SHA256 файлов.
- `config/logging.json`, `runtime/privacy.py` — приватное логирование и аудит запросов.
- `scripts/download_model.py` — проверка файлов и загрузка отсутствующих/повреждённых.
- `scripts/entrypoint.sh` — выполняется внутри контейнера перед сервером.
- `scripts/run.sh` — выполняется на компьютере, задаёт параметры Docker.
- `scripts/init-volume.sh` — разовая подготовка прав на постоянное хранилище.
- `scripts/check_service.py` — проверка запущенного API, авторизации и логов.
- `tests/test_runtime.py` — локальные тесты без скачивания модели и без Docker.

## Сборка

Из корня репозитория:

```bash
sudo docker build -t qwen-serving:dev .
```

Базовый CPU-образ закреплён по digest, поэтому неизменны и системные библиотеки,
и поставляемые в нём Python-пакеты. Сборка сверяет **все** установленные версии
с `requirements.txt` и завершается ошибкой при несовпадении, лишнем или
отсутствующем пакете. CPU-сборки vLLM, PyTorch и Triton уже находятся в базовом
образе; повторное разрешение зависимостей через `pip install` не выполняется.
Новых Python-зависимостей приложение не добавляет.

Lock получен из работающего базового окружения командой
`python -m pip list --format=freeze`. Это не переносимый рецепт установки
через обычный PyPI: некоторые CPU-пакеты собраны авторами образа. При обновлении
базы нужно одновременно обновить digest, экспортировать lock из новой базы,
пересобрать образ и повторить проверки. Сборка по одному изменяемому тегу
без digest не используется.

## Локальные секреты

Создайте `.env`, если его ещё нет:

```bash
umask 077
python3 -c 'import secrets; print("VLLM_API_KEY=" + secrets.token_hex(32)); print("HF_TOKEN=")' > .env
```

Эта команда перезаписывает `.env`: не выполняйте её поверх настроенного файла,
если хотите сохранить существующий токен. Образец полей — `.env.example`.
Формат — обычные строки `NAME=value`, без `export`, кавычек и shell-подстановок.
`.env` не попадает ни в Git, ни в Docker-контекст. Не присылайте его содержимое.

`VLLM_API_KEY` обязателен: контейнер откажется запускать сервер с пустым значением.
`HF_TOKEN` необязателен для этой публичной Qwen, но поддерживается для доступа
к gated/private моделям. Смена модели также требует обновить manifest.
После подготовки файлов HF-токен удаляется из окружения процесса сервера.
В Kubernetes эти значения нужно передавать через Secret.

## Хранилище и запуск

```bash
sudo ./scripts/init-volume.sh
sudo ./scripts/run.sh
```

Инициализация только создаёт volume `qwen-models` и назначает владельца
его корневого каталога — 10001:10001. Служебный `chown` выполняется от root;
сервер и загрузчик всегда работают от `app`. Если этот volume уже подготовлен,
первую команду можно пропустить. Не используйте рекурсивный chown чужих файлов.

При старте загрузчик проверяет размеры и SHA256 всех семи файлов модели.
Если они совпадают, даже старый кеш без `.download_complete` используется
без сетевых запросов. Если файл отсутствует или повреждён, скачивается только
он из закреплённого commit. Незавершённый файл не считается готовым. Повторная
попытка восстановит его; параллельные загрузчики используют файловую блокировку.

Затем включается offline-режим HF/Transformers и запускается vLLM. Проверка
контрольных сумм читает около 3.1 ГБ с диска и может занять несколько секунд.
Права записи на volume нужны загрузчику и его файлу блокировки.

API опубликовано только на `127.0.0.1:8000` компьютера. Порт сервера внутри
контейнера — 8000. Дождитесь успешной проверки:

```bash
curl --noproxy '*' --max-time 10 -i http://127.0.0.1:8000/health
```

Ожидается `200 OK`. `/health` открыт без API-токена; это endpoint состояния,
а не генерации. Docker healthcheck имеет 15 минут начального периода для загрузки
модели; он сообщает состояние, но сам не перезапускает контейнер.

Остановка — Ctrl+C в терминале сервера либо `sudo docker stop qwen`.
Контейнер удаляется благодаря `--rm`, volume сохраняется. Повторный запуск —
снова `sudo ./scripts/run.sh`.

## Тестовый запрос

Во втором терминале можно открыть Python и выполнить:

```python
import json
from pathlib import Path
import urllib.request

token = next(line.split('=', 1)[1] for line in Path('.env').read_text().splitlines()
             if line.startswith('VLLM_API_KEY='))
payload = {
    'model': 'qwen',
    'messages': [{'role': 'user', 'content': 'Ответь по-русски: что такое Docker?'}],
    'max_tokens': 50,
}
req = urllib.request.Request(
    'http://127.0.0.1:8000/v1/chat/completions',
    data=json.dumps(payload).encode(),
    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(req, timeout=300) as response:
    print(json.load(response)['choices'][0]['message']['content'])
```

Выполняйте из корня проекта. На CPU генерация может быть медленной; timeout
в примере — 5 минут. Потоковая выдача поддерживается API, но сама вычисления
не ускоряет. Запрос к `/v1/models` или генерации без правильного токена даёт 401.

## Настройки запуска

`CMD` задаёт стартовый набор: `float32`, контекст 1024, один одновременный запрос,
`--enforce-eager`. Можно передать свой **полный** набор аргументов после образа
или скрипта: Docker заменяет весь CMD.

```bash
sudo ./scripts/run.sh --served-model-name qwen --host 0.0.0.0 --port 8000 --dtype float32 --max-model-len 512 --max-num-seqs 1 --enforce-eager
```

Параметры Docker задаются окружением скрипта. Например:

```bash
sudo env MODEL_VOLUME=qwen-test HOST_PORT=8001 CONTAINER_NAME=qwen-test ./scripts/run.sh
```

Также доступны `IMAGE`, `ENV_FILE`, `OMP_NUM_THREADS`, `VLLM_CPU_KVCACHE_SPACE`.
При `sudo` передавайте значения через `sudo env`, как в примере, чтобы они
не потерялись. Другой volume сначала подготовьте через `init-volume.sh`
с тем же `MODEL_VOLUME`.

Входные аргументы, которые меняют ключ API или включают небезопасное
логирование, отвергаются. Политика приватности добавляется entrypoint после
пользовательских параметров. Ключ API передаётся только окружением.

## Приватные логи

vLLM и Uvicorn используют наш JSON-formatter. Произвольные сообщения,
аргументы logger, stack dumps и тексты исключений не печатаются: даже ошибка
движка может содержать промпт. Для таких событий остаются уровень, источник
и место в коде. Это намеренный компромисс: служебные ошибки менее подробны.
Не включайте сырые DEBUG-логи с реальной перепиской.

ASGI middleware отдельно записывает `request_started`, `request_finished`
или `request_failed`, случайный внутренний request ID, известный маршрут,
метод, HTTP-статус и длительность. Оно не читает текст запросов/ответов,
заголовки или query string. Неизвестные пути заменяются на `other`.
События видны для успешных, неавторизованных и ошибочных запросов.

```bash
sudo docker logs --tail 20 qwen
```

Серверу передаются явные флаги отключения логирования запросов и генераций;
дополнительно отключены стандартный access log и отладочный вывод ответов.
Приватность проверяется как локально, так и отдельной проверкой живого API.
Эта политика относится к поставляемому образу и entrypoint; не обходите их
через `--entrypoint` для рабочего сервера.

## Проверки перед merge

Локально без Docker и сторонних пакетов:

```bash
python3 -m unittest discover -s tests -v
bash -n scripts/run.sh scripts/entrypoint.sh scripts/init-volume.sh
```

При работающем контейнере, из корня проекта:

```bash
sudo python3 scripts/check_service.py
```

Проверяются пользователь контейнера, health, отсутствие/ошибка/правильность
API-токена, короткая генерация, некорректный запрос и приватность логов.
Секреты, промпт и ответ не выводятся в отчёт проверки. На генерацию отводится
до 300 секунд; это функциональная проверка, не нагрузочный тест.

Проверка соответствия lock внутри запущенного контейнера:

```bash
sudo docker exec qwen python /app/scripts/verify_dependencies.py /app/requirements.txt
```

Проверка кеша при полностью отключённой сети, после остановки сервера:

```bash
sudo docker run --rm --network none -e HF_HUB_OFFLINE=1 --mount source=qwen-models,target=/models --entrypoint python qwen-serving:dev /app/scripts/download_model.py
```

Ожидается `Model verified locally; no network access required.`
Проверка пустого хранилища, не затрагивая существующую модель:

```bash
sudo env MODEL_VOLUME=qwen-first-start ./scripts/init-volume.sh
sudo env MODEL_VOLUME=qwen-first-start CONTAINER_NAME=qwen-first-start HOST_PORT=8001 ./scripts/run.sh
```

Этот сценарий действительно скачивает ещё одну копию модели. Для него нужны
дополнительные 3.1 ГБ и сеть. После первого запуска остановите контейнер
и повторите команду с тем же volume: сетевого скачивания быть не должно.
Удаление тестового volume, только когда он больше не нужен:

```bash
sudo docker volume rm qwen-first-start
```

## Документация

- [CPU installation vLLM 0.27.0](https://docs.vllm.ai/en/v0.27.0/getting_started/installation/cpu/)
- [vLLM serve arguments](https://docs.vllm.ai/en/v0.27.0/cli/serve/)
- [Qwen at the pinned revision](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct/tree/989aa7980e4cf806f80c7fef2b1adb7bc71aa306)
- [Hugging Face download API](https://huggingface.co/docs/huggingface_hub/guides/download)
- [Dockerfile reference](https://docs.docker.com/reference/dockerfile/)
