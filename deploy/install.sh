#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$project_dir"
run_as=()
service_user=""
if [[ "$(id -u)" -eq 0 ]]; then
  service_user="${SUDO_USER:-$(stat -c '%U' "$project_dir")}"
  if [[ "$service_user" == root ]] || ! id "$service_user" >/dev/null 2>&1; then
    echo '请从项目所属的普通用户账户执行 sudo ./deploy/install.sh。' >&2
    exit 1
  fi
  run_as=(runuser -u "$service_user" --)
  service_dir=/etc/systemd/system
  control=(systemctl)
else
  service_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  control=(systemctl --user)
fi
# Check the selected manager before changing any installed unit.
if ! "${control[@]}" show-environment >/dev/null; then
  echo '无法连接 systemd；没有用户会话总线时，请执行 sudo ./deploy/install.sh 安装系统服务。' >&2
  exit 1
fi
if [[ ! -x .venv/bin/python ]]; then "${run_as[@]}" python3 -m venv .venv; fi
"${run_as[@]}" .venv/bin/python -c 'from backend.server import config; config()'
unit_dir="$(mktemp -d)"
unit_file="$unit_dir/hiking.service"
trap 'rm -rf -- "$unit_dir"' EXIT
"${run_as[@]}" .venv/bin/python deploy/render_service.py "$project_dir" "$service_user" > "$unit_file"
systemd-analyze verify "$unit_file"
mkdir -p -- "$service_dir"
install -m 644 -- "$unit_file" "$service_dir/hiking.service"
"${control[@]}" daemon-reload
"${control[@]}" enable hiking.service
"${control[@]}" restart hiking.service
"${control[@]}" status hiking.service --no-pager
