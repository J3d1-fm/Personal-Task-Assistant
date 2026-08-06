# Safe .env loader for the automation shell entrypoints.
#
# `set -a; . .env` executes the file as bash, so an unquoted value with a
# space (APP_NAME=Personal Task Assistant) becomes a command invocation and
# kills the runner. This loader parses KEY=VALUE lines without executing
# anything: comments and non-assignment lines are skipped, one layer of
# surrounding quotes is stripped (matching python-dotenv), CR is trimmed.
#
# Usage:  . "$REPO/automation/env.sh"; load_env "$REPO/.env"

load_env() {
  local file="$1" line key value
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    [[ "$line" =~ ^[A-Za-z_][A-Za-z_0-9]*= ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    if [[ "$value" =~ ^\"(.*)\"$ ]]; then
      value="${BASH_REMATCH[1]}"
    elif [[ "$value" =~ ^\'(.*)\'$ ]]; then
      value="${BASH_REMATCH[1]}"
    fi
    export "$key=$value"
  done < "$file"
}
