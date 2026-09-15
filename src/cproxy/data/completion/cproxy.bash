# cproxy 的 bash 补全。
#
# 启用（当前会话）:  source <(cproxy completion bash)
# 永久启用:          cproxy completion bash --install
#
# 候选来自 `cproxy __complete`，分组名与节点名由它按需实时拉取；controller
# 不可达时它会在短超时后静默返回空，不会卡住 <Tab>。
_cproxy_complete() {
    local -a candidates
    # 按行读：节点名可能含空格、emoji 与 `丨`，按空白分词会把它们拆坏
    local IFS=$'\n'
    candidates=($(cproxy __complete "$COMP_CWORD" "${COMP_WORDS[@]}" 2>/dev/null))
    COMPREPLY=($(compgen -W "${candidates[*]}" -- "${COMP_WORDS[COMP_CWORD]}"))
}

complete -o nosort -o bashdefault -o default -F _cproxy_complete cproxy
