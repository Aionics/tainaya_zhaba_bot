#!/usr/bin/env bash
# Установка и обновление бота «Новогодняя Жабилизация» на сервере.
#
# Первый запуск:   REPO_URL=https://github.com/<owner>/<repo>.git bash install.sh
# Обновление:      cd ~/zhabilizacia && bash install.sh
#
# Переменные (необязательно):
#   REPO_URL   — адрес репозитория (нужен только при первой установке)
#   APP_DIR    — куда ставить (по умолчанию ~/zhabilizacia)
#   BRANCH     — ветка (по умолчанию main)
#   BOT_TOKEN, ADMIN_IDS, TZ — чтобы не вводить их вручную при первой установке
set -euo pipefail

REPO_URL="${REPO_URL:-}"
APP_DIR="${APP_DIR:-$HOME/zhabilizacia}"
BRANCH="${BRANCH:-main}"

say()  { printf '\n🐸 %s\n' "$*"; }
fail() { printf '\n❌ %s\n' "$*" >&2; exit 1; }

# ── Проверки ──
command -v git    >/dev/null || fail "Не найден git"
command -v docker >/dev/null || fail "Не найден docker"
docker compose version >/dev/null 2>&1 || fail "Не найден docker compose (нужна версия 2: «docker compose», а не «docker-compose»)"
docker info >/dev/null 2>&1 || fail "Нет доступа к docker. Добавьте пользователя в группу docker или запустите через sudo"

# Если скрипт запущен из уже склонированной папки — работаем в ней
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SCRIPT_DIR" ] && [ -d "$SCRIPT_DIR/.git" ] && [ -f "$SCRIPT_DIR/bot.py" ]; then
    APP_DIR="$SCRIPT_DIR"
fi

# ── Код ──
if [ -d "$APP_DIR/.git" ]; then
    say "Обновляю код в $APP_DIR"
    git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
else
    [ -n "$REPO_URL" ] || fail "Укажите адрес репозитория: REPO_URL=https://github.com/<owner>/<repo>.git bash install.sh"
    say "Скачиваю код в $APP_DIR"
    git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"
mkdir -p data

# ── Настройки (.env создаётся один раз и больше не трогается) ──
ask() {  # ask <переменная> <вопрос>
    local var="$1" prompt="$2" value="${!1:-}"
    if [ -z "$value" ]; then
        [ -r /dev/tty ] || fail "Задайте $var через переменную окружения"
        read -rp "$prompt" value </dev/tty
    fi
    printf '%s' "$value"
}

if [ ! -f .env ]; then
    say "Первая настройка"
    token="$(ask BOT_TOKEN 'Токен бота от @BotFather: ')"
    [[ "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || fail "Токен выглядит неправильно"
    admins="$(ask ADMIN_IDS 'Telegram ID организатора (несколько — через запятую): ')"
    [[ "$admins" =~ ^[0-9]+(,[0-9]+)*$ ]] || fail "ID должны быть числами через запятую"
    tz="${TZ:-Europe/Moscow}"
    umask 077
    cat > .env <<EOF
BOT_TOKEN=$token
ADMIN_IDS=$admins
DB_PATH=/app/data/zhabilizacia.db
TZ=$tz
EOF
    chmod 600 .env
    say "Настройки сохранены в $APP_DIR/.env"
else
    say "Настройки уже есть ($APP_DIR/.env) — оставляю как есть"
fi

# ── Запуск ──
say "Собираю и запускаю контейнер"
docker compose up -d --build --remove-orphans

if command -v systemctl >/dev/null && ! systemctl is-enabled docker >/dev/null 2>&1; then
    say "⚠️  Docker не стоит в автозагрузке. Чтобы бот поднимался после отключения света, выполните:"
    echo "    sudo systemctl enable docker"
fi

say "Жду подключения к Telegram…"
for _ in $(seq 1 20); do
    if docker compose logs bot 2>/dev/null | grep -q "Run polling for bot"; then
        docker compose logs --tail 5 bot
        say "Готово! Бот работает. Лог: cd $APP_DIR && docker compose logs -f"
        exit 0
    fi
    sleep 1
done
docker compose logs --tail 30 bot
fail "Бот не подключился за 20 секунд — посмотрите лог выше"
