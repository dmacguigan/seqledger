#!/bin/bash
# Enforce the devel workflow for Claude's git commands.
# Only "devel" may be pushed; no commits/merges on main.
# Override (user-approved small change to main): include ALLOW_MAIN=1 in the command.
in=$(cat)
cmd=$(printf '%s' "$in" | jq -r '.tool_input.command // empty')
cwd=$(printf '%s' "$in" | jq -r '.cwd // empty')
[ -z "$cmd" ] && exit 0
case "$cmd" in *ALLOW_MAIN=1*) exit 0 ;; esac

block() { echo "git_guard: $1 (devel workflow; see CLAUDE.md). Override only if the user explicitly approved: prefix with ALLOW_MAIN=1." >&2; exit 2; }
branch=$(git -C "${cwd:-.}" rev-parse --abbrev-ref HEAD 2>/dev/null)

# Each git invocation in the command, split on shell separators
while IFS= read -r seg; do
  seg=$(printf '%s' "$seg" | sed -E 's/^[[:space:]]*(rtk[[:space:]]+)?//')
  case "$seg" in git\ *|git) ;; *) continue ;; esac
  # drop global options like -C <dir>, -c k=v
  args=$(printf '%s' "$seg" | sed -E 's/^git((\s+-[Cc]\s+\S+)|(\s+--\S+))*\s*//')
  sub=${args%% *}
  case "$sub" in
    push)
      rest=$(printf '%s' "$args" | sed -E 's/^push\s*//; s/[0-9]*[<>]+&?\s*\S*//g')
      pos=()
      for a in $rest; do case "$a" in -*) ;; *) pos+=("$a") ;; esac; done
      case "$rest" in *--tags*|*--all*|*--mirror*) block "push of tags/all/mirror" ;; esac
      if [ ${#pos[@]} -le 1 ]; then
        [ "$branch" = "devel" ] || block "push of branch '$branch'"
      else
        for r in "${pos[@]:1}"; do
          r=${r#+}; dst=${r##*:}; [ "$dst" = "HEAD" ] && dst=$branch
          dst=${dst#refs/heads/}
          [ "$dst" = "devel" ] || block "push to '$dst'"
        done
      fi ;;
    commit|merge|cherry-pick|rebase|revert|am)
      [ "$branch" = "main" ] && block "git $sub on main" ;;
  esac
done < <(printf '%s\n' "$cmd" | sed -E 's/(&&|\|\||;|\|)/\n/g')
exit 0
