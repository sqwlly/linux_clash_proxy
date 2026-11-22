# cproxy 的 bash 补全。
#
# 启用（当前会话）:  source <(cproxy completion bash)
# 永久启用:          cproxy completion bash --install
#
# 候选来自 `cproxy __complete`，分组名与节点名由它按需实时拉取；controller
# 不可达时它会在短超时后静默返回空，不会卡住 <Tab>。
_cproxy_complete() {
    local -a raw_candidates=()
    local cur="${COMP_WORDS[COMP_CWORD]:-}"
    local item escaped
    local meta_re='[][[:space:];|&$`'\''"\()<>?*{}!#~]'
    COMPREPLY=()

    # 使用 mapfile -t 按行读取，完全避免词拆分、通配符展开与 compgen -W 的二次命令求值
    mapfile -t raw_candidates < <(cproxy __complete "$COMP_CWORD" "${COMP_WORDS[@]}" 2>/dev/null)

    for item in "${raw_candidates[@]}"; do
        [[ -n "$item" ]] || continue
        if [[ "$cur" == \'* && "$item" != *\'* ]]; then
            COMPREPLY+=("$item")
        elif [[ "$item" =~ $meta_re ]]; then
            printf -v escaped '%q' "$item"
            COMPREPLY+=("$escaped")
        else
            COMPREPLY+=("$item")
        fi
    done
}

complete -o nosort -o bashdefault -o default -F _cproxy_complete cproxy
