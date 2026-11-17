---
name: rex-omp-workspace
description: Use for the rex-omp right-hand workspace sidebar, project scripts, or setting up project dev, start, build, and editor commands in Rex.
---

# Rex OMP workspace

Read `skill://rex` first and follow its installed-CLI guidance. Use that skill for Rex session, tab, pane, host, and client operations. This skill covers the native rex-omp workspace and its project-command interface.

## Keep the native workspace

Lua owns layout and interaction. The left pane stays the native OMP conversation. The right sidebar defaults to 20% of the tab and shows project information and agents belonging to the current user task. The lower-right agent detail uses the native read-only OMP renderer, not a second conversation or a log imitation.

Workspace tab selection changes the owning Rex client's selected tab only. Do not activate the macOS application, broadcast selection to other clients, or change Remote Control permissions. Show client errors instead of bypassing them.

Show and hide the sidebar with the workspace request interface below. Neither action should stop development services. Do not close service panes, kill processes, or restart Rex to hide or refresh the sidebar. Installation updates must also leave existing services alone.

The workspace observer does not need model calls. Do not add a model loop or repeatedly capture screens to keep agents, project status, or service information current. Inspect only what the user's task needs.

**Add script** explicitly asks this OMP session's AI to inspect and curate commands. It consumes tokens only for that user-requested work; it does not create another conversation or enable background AI polling. Busy sessions queue the request after their current task. Older running OMP sessions need a new `rex-omp` conversation for this native action.

## Curate before configuring

Project-script discovery reads existing `package.json` scripts and lockfiles, `.claude/launch.json`, `Makefile`, `justfile`, and script files at the project/package root and under `scripts/`, `script/`, `bin/`, and `tools/` (up to three directory levels). It skips symlinks, hidden/generated/dependency directories and reports scan limits. Root shell scripts, workflow-named scripts, executable scripts, and conventional script-directory entries are discovered without running them. TypeScript files need a declared shebang; do not choose a runner merely because it is installed. Discovery must not execute repository commands, install dependencies, start services, or evaluate build files as a way to list commands.

Read the project's actual configuration, dependencies, documentation and relevant script contents. Check its package-manager choice and tools on the project host. Choose the few commands most useful for everyday work, usually 3–5 and never more than 6. Preserve their useful ordering. Candidates do not appear in the sidebar until AI selects and saves them. Do not fill unused slots, guess commands, or select everything discovered.

Inspect existing build/start/compile scripts, their arguments and README/build references. Reuse the actual file with required flags and the correct working directory instead of generating a second implementation. Prefer established package/Make/Just/launch wrappers. Save a complete, portable invocation when parameters are required; never run a script or its `--help` option just to discover it.

Candidate discovery follows file additions/removals and content/interpreter changes invalidate old execution IDs. It does not automatically pin new files. Legacy private commands remain available as review candidates, not automatic sidebar selections. A file entry does not establish that its arguments, credentials or interactive input are optional.

When the user asks to configure a workflow, inspect and reconcile the complete selected list. Keep important existing choices unless the project evidence requires changing them. Discovery, selection and saving remain separate from execution.

Commands run on the host containing the project. A remote terminal editor runs remotely. A remote GUI launcher does not open an editor on the user's local Mac. Use the Rex host guidance when the project is elsewhere, and describe the actual host rather than silently falling back to this machine.

Do not write credentials, tokens, or secret environment values into saved commands. Report missing configuration instead. Discovery or saving alone never authorizes execution. Execute only for the requested task, its necessary verification, or an explicit Run confirmation; never start a server, publish, or commit merely to populate the sidebar.

## Inspect and replace the shared selection

Find the absolute filesystem path of this loaded `SKILL.md` from the skill read result. Set `skill_dir` to its containing directory, not to a user-wide skill directory or a mutable deployment symlink. Resolve the worker relative to this skill's immutable runtime source:

```sh
module_dir=$(cd -- "$skill_dir/../.." && pwd -P) || exit
worker="$module_dir/worker.py"
project_dir="$PWD" # Use the actual absolute project directory on this host.
python3 "$worker" scripts --cwd "$project_dir" list
```

`list` returns only selected `scripts`, `errors`, and `project` metadata. Use `discover` to inspect all static and legacy `candidates` together with the current selection:

```sh
python3 "$worker" scripts --cwd "$project_dir" discover
```

Read `errors`, inspect the actual files, and take `project.revision` from this result. Save the complete AI-selected list, not an append-only inventory:

```sh
python3 "$worker" scripts --cwd "$project_dir" set --revision "$revision" <<'JSON'
{"scripts":[{"name":"Build","command":"./scripts/build.sh","cwd":"."}]}
JSON
```

The example is a schema illustration, not a command to assume exists. Use only verified project commands. `cwd` is relative to the current checkout root; do not embed checkout-specific absolute paths or secrets. Saving never executes a command or changes project manifests.

Configuration is private, versioned and keyed by the canonical Git common directory. All worktrees of the same repository read the same selection; commands execute in the current checkout, never another worktree. Non-Git projects use their directory identity. A revision conflict means another worktree changed the selection: discover again and reconcile before saving. Do not blindly overwrite it. Legacy configuration files are preserved for review.

Detected GUI editors already have an **Open in …** action; do not save a duplicate launcher. Terminal editors such as `nvim` need an interactive project terminal. Never save them as background scripts: project scripts have no interactive standard input.

## Explicit workspace actions

These requests need an active workspace matching the Rex session and main OMP anchor. Use the actual inherited IDs for "here", or resolve the user's named target using `skill://rex` guidance. Send one JSON object on standard input:

```sh
printf '%s\n' '{"type":"show"}' |
  python3 "$worker" request --session "$REX_SESSION" \
    --anchor "$REX_BLOCK" --view panel

printf '%s\n' '{"type":"hide"}' |
  python3 "$worker" request --session "$REX_SESSION" \
    --anchor "$REX_BLOCK" --view panel
```

After an explicit request to run a particular command, take its exact `id` from `scripts ... list`, put it in `script_id`, and send:

```sh
jq -nc --arg id "$script_id" '{type:"run-script",id:$id}' |
  python3 "$worker" request --session "$REX_SESSION" \
    --anchor "$REX_BLOCK" --view panel
```

To open a project terminal when requested:

```sh
printf '%s\n' '{"type":"open-terminal"}' |
  python3 "$worker" request --session "$REX_SESSION" \
    --anchor "$REX_BLOCK" --view panel
```

For a detected GUI editor, use `{"type":"open-editor"}` through the same request interface. A project terminal is reused and selected inside Rex; hiding the sidebar does not close it.

To set the sidebar width on an existing conversation, send `{"type":"set-width","width":20}` through the same request interface. Width is an integer percentage from 15 to 35. This saves a workspace override, applies the actual Rex split, and survives observer restart without restarting the conversation or its agents. The controller also corrects the split once after the GUI mounts a new observer pane. Ordinary status updates do not undo subsequent manual border dragging; an explicit show restores the configured width.

Read the response and report failures. After a timeout, inspect existing state before retrying so a successful first request does not create duplicate terminals or services. A launch acknowledgement alone does not prove that a server is ready or a build passed. Verify only when the task calls for it, using the returned target and the installed Rex CLI guidance.

Project-script discovery is not Claude Desktop's embedded Browser, automatic verification, or full `autoPort` implementation. Do not claim those capabilities or silently add browser automation, port rewriting, or verification loops.
