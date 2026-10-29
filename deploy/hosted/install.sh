#!/usr/bin/env bash
# Install or upgrade a self-hosted Agora server from the published images.
#
#   mkdir -p agora && cd agora
#   curl -fsSLO https://raw.githubusercontent.com/Devin333/Agora-Hub/main/deploy/hosted/install.sh
#   bash install.sh
#
# The script downloads compose.yaml, Caddyfile and .env.example next to itself,
# creates .env with freshly generated keys (an existing .env is kept, so a rerun
# upgrades in place), pulls the images and starts the stack.
#
# Options:
#   --domain <name>     the domain users open (otherwise asked for)
#   --tag <tag>         image tag, e.g. v0.1.2 (default: edge, or the one in .env)
#   --registry <path>   ghcr.io/devin333 (default) or the ACR mirror
#   --mirror-cn         shorthand for the Alibaba Cloud ACR mirror
#   --local             use the files already next to this script, download nothing
#   --prepare-only      write the files and .env, then stop before pulling
set -euo pipefail

SOURCE_BASE="${AGORA_INSTALL_SOURCE:-https://raw.githubusercontent.com/Devin333/Agora-Hub/main/deploy/hosted}"
GHCR_REGISTRY="ghcr.io/devin333"
ACR_REGISTRY="registry.cn-hangzhou.aliyuncs.com/agora-hub"
FILES=(compose.yaml Caddyfile .env.example)

domain=""
tag=""
registry=""
local_files=0
prepare_only=0

die() { printf '错误：%s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) domain="${2:-}"; shift 2 ;;
    --tag) tag="${2:-}"; shift 2 ;;
    --registry) registry="${2:-}"; shift 2 ;;
    --mirror-cn) registry="$ACR_REGISTRY"; shift ;;
    --local) local_files=1; shift ;;
    --prepare-only) prepare_only=1; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) die "不认识的参数：$1" ;;
  esac
done

cd "$(dirname "$0")"

require() { command -v "$1" >/dev/null 2>&1 || die "需要 $1，请先安装。"; }
require docker
require openssl
docker compose version >/dev/null 2>&1 || die "需要 Docker Compose 插件（docker compose）。"
docker info >/dev/null 2>&1 || die "Docker 没有运行，或当前用户没有权限访问 Docker。"

# --- files -------------------------------------------------------------------

if [ "$local_files" -eq 0 ]; then
  require curl
  for file in "${FILES[@]}"; do
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
      --retry 3 --output "$file.download" "$SOURCE_BASE/$file"
    mv "$file.download" "$file"
  done
  note "已下载 ${FILES[*]}。"
else
  for file in "${FILES[@]}"; do
    [ -f "$file" ] || die "--local 需要 $file 在脚本旁边。"
  done
fi

# --- .env --------------------------------------------------------------------

# 32 random bytes in the two encodings the server checks: the credential key is
# standard base64, the activity key is a Fernet key (URL-safe base64).
credential_key() { openssl rand -base64 32 | tr -d '\n'; }
activity_key() { openssl rand -base64 32 | tr -d '\n' | tr '+/' '-_'; }

env_value() { sed -n "s/^$1=//p" .env | tail -n 1; }
set_env_value() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp .env.XXXXXX)"
  if grep -q "^$key=" .env; then
    awk -v key="$key" -v value="$value" -F= \
      'BEGIN { OFS = "=" } $1 == key { print key, value; next } { print }' .env >"$tmp"
  else
    cat .env >"$tmp"
    printf '%s=%s\n' "$key" "$value" >>"$tmp"
  fi
  chmod 600 "$tmp"
  mv "$tmp" .env
}

if [ -L .env ]; then
  die ".env 是符号链接，拒绝写入。请换成普通文件。"
fi

created_env=0
if [ ! -f .env ]; then
  (umask 077 && cp .env.example .env)
  created_env=1
fi
chmod 600 .env

if [ -z "$(env_value NEWSROOM_CREDENTIAL_ENCRYPTION_KEY)" ]; then
  set_env_value NEWSROOM_CREDENTIAL_ENCRYPTION_KEY "$(credential_key)"
  generated_keys=1
fi
if [ -z "$(env_value NEWS_ACTIVITY_ENCRYPTION_KEY)" ]; then
  set_env_value NEWS_ACTIVITY_ENCRYPTION_KEY "$(activity_key)"
  generated_keys=1
fi

current_domain="$(env_value NEWSROOM_DOMAIN)"
if [ -z "$domain" ] && { [ -z "$current_domain" ] || [ "$current_domain" = "agora.example.com" ]; }; then
  if [ -t 0 ]; then
    read -r -p "用户访问的域名（不带 https://，例如 agora.example.com）：" domain
  fi
  [ -n "$domain" ] || die "需要域名。用 --domain 指定，或在 .env 里填写 NEWSROOM_DOMAIN。"
fi
if [ -n "$domain" ]; then
  case "$domain" in
    *://*|*/*|*" "*) die "域名只填主机名本身，不要带协议、路径或空格：$domain" ;;
  esac
  set_env_value NEWSROOM_DOMAIN "$domain"
fi
[ -n "$tag" ] && set_env_value AGORA_IMAGE_TAG "$tag"
[ -n "$registry" ] && set_env_value AGORA_IMAGE_REGISTRY "$registry"

if [ "$created_env" -eq 1 ]; then
  note "已创建 .env（权限 600）。"
fi
if [ "${generated_keys:-0}" -eq 1 ]; then
  note "已生成主密钥。请把 .env 复制一份保存在这台服务器以外：丢失后，所有用户保存的模型 key 和研究记录都无法解密。"
fi

docker compose config --quiet || die "compose 配置校验失败，请检查 .env。"

if [ "$prepare_only" -eq 1 ]; then
  note "文件已就绪。检查 .env 后运行：bash install.sh --local"
  exit 0
fi

# --- start -------------------------------------------------------------------

note "拉取镜像（$(env_value AGORA_IMAGE_REGISTRY)，标签 $(env_value AGORA_IMAGE_TAG)）…"
docker compose pull
note "启动服务…"
docker compose up -d --wait --wait-timeout 300

note ""
note "服务已启动：https://$(env_value NEWSROOM_DOMAIN)/paper-agent"
note "第一次部署时，创建管理员账号："
note "  docker compose exec api news accounts create-admin <用户名>"
