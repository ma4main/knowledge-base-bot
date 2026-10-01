# Развёртывание с нуля: инструкция по шагам

Написано так, чтобы по ней мог работать техник без знания проекта. Каждый шаг
содержит команды и признак, что он удался. Что это за система: [`ARCHITECTURE.md`](ARCHITECTURE.md).
Что делать после запуска: [`OPERATIONS.md`](OPERATIONS.md).

## 0. Что нужно на входе

| Что | Где взять |
|---|---|
| Linux-сервер с Docker ≥ 24, Docker Compose v2 и git | любой VPS: 1 vCPU, 1 ГБ памяти, 10 ГБ диска |
| Исходящий доступ к `api.telegram.org:443`, `openrouter.ai:443`, `github.com:22` | входящие порты не нужны |
| Telegram-бот и его токен | @BotFather → `/newbot` |
| Ключ OpenRouter с балансом | https://openrouter.ai/keys, задайте лимит расхода |
| Приватный репозиторий на GitHub | форк или копия этого репозитория |
| Числовой Telegram ID первого руководителя | бот ответит на `/whoami` даже без доступа; или @userinfobot |

Задайте переменные для команд ниже:

```bash
export KB_DIR=/opt/kb-bot
export REPO_URL=git@github.com:<организация>/<репозиторий>.git
```

## 1. Настроить бота в BotFather

1. `/newbot`, сохранить токен.
2. `/mybots` → бот → Bot Settings → **Group Privacy → Turn off**. Без этого бот не
   видит сообщения в рабочих чатах.
3. По желанию: `/setcommands` со списком `menu`, `whoami`, `help`.

## 2. Подготовить репозиторий

Создайте приватный репозиторий и залейте в него содержимое этого. Приватность
обязательна: бот будет отправлять туда рабочие чаты, файлы и снимок состояния
с именами сотрудников. Затем замените
демонстрационную базу своей или начните с пустых разделов: см. раздел 9.

## 3. Проверить, что на сервере нет конфликтов

```bash
docker --version && docker compose version && git --version
docker compose ls                                     # не должно быть проекта "bot"
docker ps -a --format '{{.Names}}' | grep -x kb-bot   # пусто
df -h / | tail -1
```

Если compose-проект `bot` уже существует, на шаге 6 добавьте в `bot/.env` строку
`COMPOSE_PROJECT_NAME=kb-bot`. Тогда том будет называться `kb-bot_kb-bot-data`.

## 4. Deploy-ключ с правом записи

Бот сам коммитит и пушит, поэтому ключ нужен на запись. Имена файлов зафиксированы
в `bot/docker-compose.yml`, они монтируются в контейнер:

```bash
ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519_github -N "" -C "kb-bot deploy key"
cat >> /root/.ssh/config <<'EOF'
Host github.com
    HostName github.com
    User git
    IdentityFile /root/.ssh/id_ed25519_github
    IdentitiesOnly yes
EOF
chmod 600 /root/.ssh/config
ssh-keyscan github.com >> /root/.ssh/known_hosts 2>/dev/null
cat /root/.ssh/id_ed25519_github.pub
```

Публичный ключ добавить в репозиторий: GitHub → Settings → Deploy keys → Add,
галочка **Allow write access**. Через `gh`:

```bash
gh repo deploy-key add /root/.ssh/id_ed25519_github.pub --repo <организация>/<репозиторий> --allow-write --title "kb-bot server"
```

Проверка:

```bash
ssh -T git@github.com     # ожидается: "Hi <repo>! You've successfully authenticated"
```

## 5. Клонировать и задать git-личность бота

```bash
git clone "$REPO_URL" "$KB_DIR"
cd "$KB_DIR" && git config user.name "KB Bot" && git config user.email "bot@example.local" && git config pull.rebase true
```

Признак: `ls "$KB_DIR"` показывает `knowledge bot docs`.

## 6. Секреты и первый руководитель

```bash
"$KB_DIR"/bot/set-secrets.sh          # спросит токен бота и ключ OpenRouter, ввод скрыт
```

Затем в `bot/.env` вписать числовой Telegram ID первого руководителя:

```
LEADER_IDS=123456789
```

Он импортируется в состояние бота один раз при первом запуске. Дальше руководители
назначаются из меню бота. Остальные переменные описаны в [`CONFIG.md`](CONFIG.md),
для старта менять их не нужно.

## 7. Проверки без запуска

Собирает образ и гоняет самопроверку в отдельном контейнере, ключи не нужны:

```bash
cd "$KB_DIR"/bot && docker compose build
docker run --rm -e KB_ROOT=/app/kb -v "$KB_DIR":/app/kb:ro -v "$KB_DIR"/bot:/app/bot bot-kb-bot python selfcheck.py 2>&1 | tail -3
```

Признак: последняя строка `Проверки пройдены.`

## 8. Запуск

```bash
cd "$KB_DIR"/bot && docker compose up -d --build
sleep 20 && docker compose logs --tail 40
```

В логе должны быть строки:
- `База знаний загружена: N единиц`;
- `Запущен @<имя_бота> | модель … | руководителей: 1`.

Через 1–2 минуты:

```bash
docker ps --filter name=kb-bot --format '{{.Names}} {{.Status}}'   # kb-bot Up … (healthy)
```

Если `unhealthy` или контейнер перезапускается: `docker compose logs --tail 100`.
Типичные причины: пустой токен, нет сети до `api.telegram.org`, `KB_ROOT` не на базу.

Проверка живьём, с аккаунта руководителя:

1. `/whoami`: бот отвечает ID и ролью «руководитель».
2. `/menu`: приходит меню кнопками, «Люди и доступ» показывает состав.
3. Любой вопрос по базе: ответ за 5–15 секунд. В логе `стоимость=$…`.
4. Меню → Управление → Чаты → «Отправить сырьё в git сейчас», затем на сервере:

```bash
cd "$KB_DIR" && git log origin/main..HEAD --oneline   # пусто, значит всё уехало
```

Если push не прошёл, в логе будет `git push не прошёл`: проверить право записи у
deploy-ключа и адрес `git remote -v`.

## 9. Своя база вместо демонстрационной

Демонстрационная база описывает вымышленное агентство «Маяк». Чтобы бот работал
для вашей команды:

1. Отредактируйте `knowledge/_config.json`: название компании, отдел, продукты,
   разделы с диапазонами id и описаниями, примеры вопросов. Формат описан в
   [`CONFIG.md`](CONFIG.md).
2. Удалите демонстрационные единицы и создайте папки под свои разделы. Имена
   папок совпадают с ключами `sections` в `_config.json`.
3. Перепишите `knowledge/INDEX.md`: шапка с числом единиц, по заголовку на раздел
   со счётчиком, по строке на единицу. Формат: [`KNOWLEDGE-FORMAT.md`](KNOWLEDGE-FORMAT.md).
   Начать можно с пустых разделов: бот сам добавляет строки и правит счётчики.
4. Заполните `knowledge/PEOPLE.md` (кто за что отвечает) и `knowledge/LINKS.md`.
5. Очистите `files/` (оставьте `README.md`) и `chats-live/`.
6. Прогоните `selfcheck.py` (шаг 7): он проверит, что каталог сходится с файлами.

Дальше базу удобнее пополнять через бота: «запомни …» в личке, тег в рабочем чате,
ночной разбор. Единицы можно править и руками: бот подхватывает изменения в
течение минуты после `git push`.

## 10. Обновление кода и обслуживание

```bash
cd "$KB_DIR" && git pull --rebase --autostash && cd bot && docker compose up -d --build
```

`--autostash` обязателен: `chats-live/` дописывается ботом постоянно. Правки
`knowledge/` руками пересборки не требуют. Раз в несколько месяцев:
`docker builder prune -af`. Остальное в [`OPERATIONS.md`](OPERATIONS.md).

## 11. Перенос на другой сервер

Состояние бота (роли, доступы, чаты) раз в сутки уезжает в репозиторий:
`bot-state/state.json`. На новом сервере с пустым томом бот поднимет его сам.
Переписку и логи, если они нужны, переносят архивом тома:

```bash
# на старом сервере
tar -czf /root/kb-bot-data-$(date +%F).tar.gz -C /var/lib/docker/volumes/bot_kb-bot-data/_data .
# на новом, до первого запуска
docker volume create bot_kb-bot-data && tar -xzf kb-bot-data-*.tar.gz -C /var/lib/docker/volumes/bot_kb-bot-data/_data
```

Старого бота остановить до запуска нового: два процесса с одним токеном ломают
получение сообщений обоим.

## 12. Откат и удаление

Остановить: `cd "$KB_DIR"/bot && docker compose down` (том и клон остаются).
Убрать полностью: `docker volume rm bot_kb-bot-data && rm -rf "$KB_DIR"`, удалить
deploy-ключ из репозитория, отозвать ключ OpenRouter.
