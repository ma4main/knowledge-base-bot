#!/usr/bin/env bash
# Ввод секретов в .env без следов в переписке и в истории команд.
#
# Запуск с рабочей машины:
#   ssh -t root@СЕРВЕР /opt/kb-bot/bot/set-secrets.sh
#
# Ввод скрыт (как пароль), значения пишутся прямо в bot/.env.
# Сам .env в git не попадает — он в .gitignore.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
    cp "$DIR/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "Создан $ENV_FILE из образца."
fi

# Подставляет значение в .env, не трогая остальные строки.
set_var() {
    local key="$1" value="$2"
    if grep -q "^${key}=" "$ENV_FILE"; then
        # Разделитель | вместо / — в токенах и ключах встречаются слэши.
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        echo "${key}=${value}" >> "$ENV_FILE"
    fi
}

ask() {
    local key="$1" prompt="$2" value=""
    read -r -s -p "$prompt: " value
    echo
    if [ -z "$value" ]; then
        echo "  пропущено, прежнее значение сохранено"
        return
    fi
    set_var "$key" "$value"
    echo "  записано (${#value} символов)"
}

echo "Ввод скрыт — на экране ничего не появится. Пустой ввод оставляет прежнее значение."
echo

ask TELEGRAM_BOT_TOKEN "Токен бота от @BotFather"
ask OPENROUTER_API_KEY "Ключ OpenRouter"

chmod 600 "$ENV_FILE"

echo
echo "Готово. Что сейчас в .env (значения скрыты):"
sed -E 's/=(.{0,4}).*/=\1…скрыто/' "$ENV_FILE" | grep -E '^[A-Z]' || true
echo
echo "Дальше: cd $DIR && docker compose up -d"
