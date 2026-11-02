# Qwen serving

Qwen2.5-1.5B-Instruct на vLLM 0.27.0, инференс строго на CPU.
Контейнер работает от пользователя `10001`. Все 155 Python-пакетов закреплены
в `requirements.txt` и проверяются при сборке; базовый образ закреплён digest.

При первом запуске `scripts/download_model.py` скачивает модель с Hugging Face
в PVC `/models/qwen`. При следующих запусках проверяет локальные файлы по SHA-256
и не обращается к сети. Затем entrypoint запускает vLLM в офлайн-режиме.
Факт запроса, статус и длительность пишутся в JSON-логи; промпты, ответы и токены
не логируются. Произвольные сообщения vLLM скрываются, включая тексты ошибок.

## Запуск в Minikube

Нужны Linux x86_64 с AVX2, Docker, Python 3, около 16 ГБ RAM и 25 ГБ места на диске.
Все команды выполняются из корня репозитория. Первый запуск требует интернета.

Создайте `.env` из `.env.example`, задайте случайный `VLLM_API_KEY`.
`HF_TOKEN` для этой публичной модели можно оставить пустым. Реальный `.env` не
попадает в Git или образ.

```bash
cp -n .env.example .env
chmod 600 .env
python3 -c 'import secrets; print(secrets.token_hex(32))'

bash scripts/install-k8s-tools.sh
sudo .tools/minikube -p qwen-serving start --force \
  --driver=docker --container-runtime=containerd \
  --kubernetes-version=v1.35.0 --cpus=4 --memory=10240
sudo .tools/minikube -p qwen-serving update-context

sudo docker build -t qwen-serving:dev .
sudo .tools/minikube -p qwen-serving image load qwen-serving:dev
sudo .tools/kubectl --context qwen-serving apply -f k8s/namespace.yaml
sudo env PATH="$PWD/.tools:$PATH" python3 scripts/create-secret.py
sudo .tools/kubectl --context qwen-serving apply -f k8s/
sudo .tools/kubectl --context qwen-serving -n qwen-serving \
  rollout status deployment/qwen --timeout=65m
```

Secret `qwen-secrets` создаётся из `.env` без вывода значений токенов.
Параметры инференса находятся в `k8s/configmap.yaml`. Для экономии RAM заданы
контекст 1024 токена, batch budget 128 и KV-кэш 256 MiB; включён chunked prefill.
После изменения ConfigMap или Secret примените манифесты и перезапустите Pod:

```bash
sudo .tools/kubectl --context qwen-serving apply -f k8s/
sudo .tools/kubectl --context qwen-serving -n qwen-serving rollout restart deployment/qwen
```

PVC `qwen-models` сохраняется при перезапуске Pod. Удаление PVC или профиля
Minikube удалит кэш. Для другого Kubernetes-кластера загрузите образ в registry,
замените `image` в Deployment и используйте свой context и подходящий StorageClass.

## Проверка API

Откройте порт и оставьте команду работать:

```bash
sudo .tools/kubectl --context qwen-serving -n qwen-serving port-forward service/qwen 8000:8000
```

В другом терминале отправьте запрос (токен читается из `.env`):

```bash
python3 - <<'PY'
import json, sys, urllib.request
sys.path.insert(0, 'scripts')
from check_service import api_key
body = json.dumps({'model': 'qwen', 'messages': [
    {'role': 'user', 'content': 'Что такое Docker? Ответь кратко по-русски.'}
], 'max_tokens': 16}).encode()
r = urllib.request.Request('http://127.0.0.1:8000/v1/chat/completions', data=body,
    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key()})
with urllib.request.urlopen(r, timeout=600) as response:
    print(json.load(response)['choices'][0]['message']['content'])
PY
```

CPU-генерация может занять несколько минут. `/health` доступен без токена для
probes; `/v1/models` и `/v1/chat/completions` требуют Bearer token.
Для диагностики используйте `get pods` и `logs deployment/qwen` в namespace
`qwen-serving`. `OOMKilled` означает нехватку памяти.

Локальные тесты: `python3 -m unittest discover -s tests -v`.
Для запуска только в Docker: `sudo bash scripts/init-volume.sh`, затем
`sudo bash scripts/run.sh` (после сборки образа).
Остановить кластер: `sudo .tools/minikube -p qwen-serving stop`.
