#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_ROOT="${1:-}"
if [[ -z "$WORKSPACE_ROOT" || ! -d "$WORKSPACE_ROOT" ]]; then
  echo "[SixBench] normalize-workspace 需要一个已存在的工作区目录。" >&2
  exit 2
fi

WORKSPACE_ROOT="$(cd -- "$WORKSPACE_ROOT" && pwd -P)"
REPOSITORY_COUNT=0

while IFS= read -r -d '' git_directory; do
  repository="${git_directory%/.git}"
  repository="$(cd -- "$repository" && pwd -P)"
  case "$repository" in
    "$WORKSPACE_ROOT"|"$WORKSPACE_ROOT"/*) ;;
    *)
      echo "[SixBench] Git 仓库逃逸出工作区：$repository" >&2
      exit 2
      ;;
  esac

  REPOSITORY_COUNT=$((REPOSITORY_COUNT + 1))
  git -C "$repository" config core.filemode true

  while IFS= read -r -d '' index_entry; do
    metadata="${index_entry%%$'\t'*}"
    relative_path="${index_entry#*$'\t'}"
    mode="${metadata%% *}"
    tracked_path="$repository/$relative_path"

    case "$mode" in
      100644)
        [[ -f "$tracked_path" && ! -L "$tracked_path" ]] || {
          echo "[SixBench] Windows 复制后缺少普通文件：$relative_path" >&2
          exit 2
        }
        chmod 0644 "$tracked_path"
        ;;
      100755)
        [[ -f "$tracked_path" && ! -L "$tracked_path" ]] || {
          echo "[SixBench] Windows 复制后缺少可执行文件：$relative_path" >&2
          exit 2
        }
        chmod 0755 "$tracked_path"
        ;;
      120000)
        link_target="$(git -C "$repository" show ":$relative_path")"
        [[ -n "$link_target" && "$link_target" != /* ]] || {
          echo "[SixBench] 拒绝恢复绝对或空符号链接：$relative_path" >&2
          exit 2
        }
        unresolved_target="$(dirname -- "$tracked_path")/$link_target"
        if resolved_target="$(realpath -m "$unresolved_target" 2>/dev/null)"; then
          :
        elif command -v python3 >/dev/null 2>&1; then
          resolved_target="$(
            python3 -c \
              'import os, sys; print(os.path.realpath(sys.argv[1]))' \
              "$unresolved_target"
          )"
        else
          echo "[SixBench] 无法安全解析符号链接：$relative_path" >&2
          exit 2
        fi
        case "$resolved_target" in
          "$WORKSPACE_ROOT"|"$WORKSPACE_ROOT"/*) ;;
          *)
            echo "[SixBench] 拒绝恢复逃逸出工作区的符号链接：$relative_path" >&2
            exit 2
            ;;
        esac
        if [[ -e "$tracked_path" || -L "$tracked_path" ]]; then
          [[ ! -d "$tracked_path" || -L "$tracked_path" ]] || {
            echo "[SixBench] 符号链接位置被目录占用：$relative_path" >&2
            exit 2
          }
          rm -f -- "$tracked_path"
        fi
        ln -s -- "$link_target" "$tracked_path"
        [[ -L "$tracked_path" && "$(readlink "$tracked_path")" == "$link_target" ]] || {
          echo "[SixBench] 符号链接恢复失败：$relative_path" >&2
          exit 2
        }
        ;;
    esac
  done < <(git -C "$repository" ls-files --stage -z)

  if [[ -n "$(git -C "$repository" status --porcelain --untracked-files=no)" ]]; then
    echo "[SixBench] Windows 复制后仓库仍有已跟踪文件漂移：$repository" >&2
    exit 2
  fi
done < <(find "$WORKSPACE_ROOT" -type d -name .git -prune -print0)

if [[ "$REPOSITORY_COUNT" -eq 0 ]]; then
  echo "[SixBench] 工作区中没有找到可验证的 Git 仓库。" >&2
  exit 2
fi

echo "[SixBench] 已校正 $REPOSITORY_COUNT 个冻结 Git 工作区的权限与符号链接。"
