#!/usr/bin/env bash

# Read JSON input from stdin
input=$(cat)

# Extract values
cwd=$(echo "$input" | jq -r '.workspace.current_dir')
model=$(echo "$input" | jq -r '.model.display_name')
transcript_path=$(echo "$input" | jq -r '.transcript_path')

# Use full path (replace home with ~)
dir="${cwd/#$HOME/~}"

# Shorten parent directories to their shortest unique prefixes.
DIR_MAX_LENGTH=80

shorten_statusline_component() {
    local component="$1"
    local parent_dir="$2"
    local shortened="${component:0:1}"
    local prefix_length
    local prefix
    local sibling
    local sibling_name
    local matching

    if [ -z "$component" ]; then
        printf "%s" "$component"
        return
    fi

    if [ ! -d "$parent_dir" ] || [ ! -r "$parent_dir" ]; then
        printf "%s" "$shortened"
        return
    fi

    for ((prefix_length = 1; prefix_length <= ${#component}; prefix_length++)); do
        prefix="${component:0:prefix_length}"
        matching=0
        for sibling in "$parent_dir"/* "$parent_dir"/.[!.]* "$parent_dir"/..?*; do
            if [ -d "$sibling" ]; then
                sibling_name="${sibling##*/}"
                case "$sibling_name" in
                    "$prefix"*) matching=$((matching + 1)) ;;
                esac
            fi
        done

        if [ "$matching" -eq 1 ]; then
            shortened="$prefix"
            break
        fi
    done

    printf "%s" "$shortened"
}

shorten_statusline_dir() {
    local display_path="$1"
    local prefix=""
    local relative_path=""
    local parent_dir=""
    local component
    local component_dir
    local shortened_component
    local component_index
    local component_count
    local last_component_index
    local -a components=()

    case "$display_path" in
        "~")
            prefix="~"
            parent_dir="$HOME"
            ;;
        "~/"*)
            prefix="~"
            relative_path="${display_path:2}"
            parent_dir="$HOME"
            ;;
        /*)
            prefix="/"
            relative_path="${display_path#/}"
            parent_dir="/"
            ;;
        *)
            relative_path="$display_path"
            parent_dir="."
            ;;
    esac

    if [ -n "$relative_path" ]; then
        IFS='/' read -r -a components <<< "$relative_path"
    fi

    statusline_short_dir="$prefix"
    component_count=${#components[@]}
    last_component_index=$((component_count - 1))

    for ((component_index = 0; component_index < component_count; component_index++)); do
        component="${components[$component_index]}"
        [ -n "$component" ] || continue

        if [ "$component_index" -eq "$last_component_index" ]; then
            shortened_component="$component"
        else
            shortened_component=$(shorten_statusline_component "$component" "$parent_dir")
        fi

        if [ -z "$statusline_short_dir" ]; then
            statusline_short_dir="$shortened_component"
        elif [ "$statusline_short_dir" = "/" ]; then
            statusline_short_dir="/${shortened_component}"
        else
            statusline_short_dir="${statusline_short_dir}/${shortened_component}"
        fi

        if [ "$component_index" -eq "$last_component_index" ]; then
            statusline_short_dir_final_start=$((${#statusline_short_dir} - ${#shortened_component}))
        fi

        if [ "$parent_dir" = "/" ]; then
            component_dir="/${component}"
        else
            component_dir="${parent_dir}/${component}"
        fi
        parent_dir="$component_dir"
    done

    if [ "$component_count" -eq 0 ]; then
        statusline_short_dir_final_start=${#statusline_short_dir}
    fi
}

color_statusline_dir() {
    local path="$1"
    local final_start="$2"
    local visible_length=${#path}
    local keep_length=$((DIR_MAX_LENGTH - 1))
    local tail_start
    local visible_prefix
    local visible_final
    local prefix_length

    if [ "$visible_length" -gt "$DIR_MAX_LENGTH" ]; then
        tail_start=$((visible_length - keep_length))
        visible_prefix="${path:tail_start:keep_length}"
        prefix_length=$((final_start - tail_start))
        [ "$prefix_length" -lt 0 ] && prefix_length=0
        [ "$prefix_length" -gt "$keep_length" ] && prefix_length="$keep_length"
        visible_final="${visible_prefix:prefix_length}"
        visible_prefix="${visible_prefix:0:prefix_length}"
        printf "%s" "\033[38;5;103m…${visible_prefix}\033[0m"
        [ -n "$visible_final" ] && printf "%s" "\033[38;5;31m${visible_final}\033[0m"
    else
        visible_prefix="${path:0:final_start}"
        visible_final="${path:final_start}"
        printf "%s" "\033[38;5;103m${visible_prefix}\033[0m"
        [ -n "$visible_final" ] && printf "%s" "\033[38;5;31m${visible_final}\033[0m"
    fi
}

shorten_statusline_dir "$dir"
dir="$statusline_short_dir"
dir_colored=$(color_statusline_dir "$dir" "$statusline_short_dir_final_start")

# Calculate token usage and cost from transcript
tokens=""
cost=""
if [ -n "$transcript_path" ] && [ -f "$transcript_path" ]; then
    # Parse transcript JSON and sum up tokens by type
    token_data=$(jq -rs '
        [.[] | select(.type == "assistant") | .message.usage | select(. != null)] |
        {
            input: ([.[].input_tokens // 0] | add),
            cache_read: ([.[].cache_read_input_tokens // 0] | add),
            cache_write: ([.[].cache_creation_input_tokens // 0] | add),
            output: ([.[].output_tokens // 0] | add)
        } | [.input, .cache_read, .cache_write, .output] | @tsv
    ' "$transcript_path" 2>/dev/null)

    if [ -n "$token_data" ]; then
        input_tok=$(echo "$token_data" | cut -f1)
        cache_read_tok=$(echo "$token_data" | cut -f2)
        cache_write_tok=$(echo "$token_data" | cut -f3)
        output_tok=$(echo "$token_data" | cut -f4)

        # Calculate total tokens
        token_sum=$((input_tok + cache_read_tok + cache_write_tok + output_tok))
        tokens=$(printf "%'d" "$token_sum")

        # Determine pricing based on model
        case "$model" in
            *"Sonnet 4.5"*|*"4.5"*)
                input_price=3
                cache_read_price=0.30
                cache_write_price=3.75
                output_price=15
                ;;
            *"Opus"*)
                input_price=15
                cache_read_price=1.50
                cache_write_price=18.75
                output_price=75
                ;;
            *"Haiku"*)
                input_price=0.25
                cache_read_price=0.03
                cache_write_price=0.30
                output_price=1.25
                ;;
            *)
                # Default to Sonnet 4.5 pricing
                input_price=3
                cache_read_price=0.30
                cache_write_price=3.75
                output_price=15
                ;;
        esac

        # Calculate cost
        cost_calc=$(echo "scale=4; ($input_tok * $input_price + $cache_read_tok * $cache_read_price + $cache_write_tok * $cache_write_price + $output_tok * $output_price) / 1000000" | bc)
        if [ -n "$cost_calc" ] && [ "$(echo "$cost_calc > 0" | bc)" -eq 1 ]; then
            cost=$(printf "\$%.2f" "$cost_calc")
        fi
    fi
fi

# Get git branch if in a git repo
if git -C "$cwd" rev-parse --git-dir > /dev/null 2>&1; then
    branch=$(git -C "$cwd" -c core.useBuiltinFSMonitor=false branch --show-current 2>/dev/null)

    # Check for upstream tracking and commits ahead/behind
    upstream=""
    if git -C "$cwd" rev-parse --abbrev-ref @{u} > /dev/null 2>&1; then
        ahead=$(git -C "$cwd" rev-list --count @{u}..HEAD 2>/dev/null)
        behind=$(git -C "$cwd" rev-list --count HEAD..@{u} 2>/dev/null)

        if [ "$ahead" -gt 0 ] && [ "$behind" -gt 0 ]; then
            upstream="↑${ahead}↓${behind}"
        elif [ "$ahead" -gt 0 ]; then
            upstream="↑${ahead}"
        elif [ "$behind" -gt 0 ]; then
            upstream="↓${behind}"
        fi
    fi

    # Check git status
    if ! git -C "$cwd" -c core.useBuiltinFSMonitor=false diff --quiet 2>/dev/null || \
       ! git -C "$cwd" -c core.useBuiltinFSMonitor=false diff --cached --quiet 2>/dev/null; then
        git_status="!"
        git_color="178"
    elif [ -n "$(git -C "$cwd" -c core.useBuiltinFSMonitor=false ls-files --others --exclude-standard 2>/dev/null)" ]; then
        git_status="?"
        git_color="76"
    else
        git_status=""
        git_color="76"
    fi

    if [ -n "$branch" ]; then
        # Build the output with branch, optional upstream, optional status
        output="${dir_colored} on \033[38;5;${git_color}m${branch}\033[0m"

        if [ -n "$upstream" ]; then
            output="${output}\033[36m${upstream}\033[0m"
        fi

        if [ -n "$git_status" ]; then
            output="${output}\033[38;5;${git_color}m${git_status}\033[0m"
        fi

        output="${output} [\033[35m${model}\033[0m"

        if [ -n "$tokens" ]; then
            output="${output} | \033[33m${tokens}\033[0m"
            if [ -n "$cost" ]; then
                output="${output} (\033[32m${cost}\033[0m)"
            fi
        fi

        output="${output}]"
        printf "%b" "$output"
    else
        output="${dir_colored} [\033[35m${model}\033[0m"

        if [ -n "$tokens" ]; then
            output="${output} | \033[33m${tokens}\033[0m"
            if [ -n "$cost" ]; then
                output="${output} (\033[32m${cost}\033[0m)"
            fi
        fi

        output="${output}]"
        printf "%b" "$output"
    fi
else
    output="${dir_colored} [\033[35m${model}\033[0m"

    if [ -n "$tokens" ]; then
        output="${output} | \033[33m${tokens}\033[0m"
        if [ -n "$cost" ]; then
            output="${output} (\033[32m${cost}\033[0m)"
        fi
    fi

    output="${output}]"
    printf "%b" "$output"
fi
