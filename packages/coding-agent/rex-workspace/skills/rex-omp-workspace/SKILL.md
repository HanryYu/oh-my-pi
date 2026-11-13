---
name: rex-omp-workspace
description: Use for the rex-omp right-hand workspace sidebar, project scripts, or setting up project dev, start, build, and editor commands in Rex.
---

# Rex OMP workspace

Read `skill://rex` first and follow its installed-CLI guidance. Use that skill for Rex session, tab, pane, host, and client operations. This skill covers the native rex-omp workspace and its project-command interface.

## Keep the native workspace

Lua owns layout and interaction. The left pane stays the native OMP conversation. The right sidebar defaults to 25% of the tab and shows project information and agents belonging to the current user task. The lower-right agent detail uses the native read-only OMP renderer, not a second conversation or a log imitation.

Workspace tab selection changes the owning Rex client's selected tab only. Do not activate the macOS application, broadcast selection to other clients, or change Remote Control permissions. Show client errors instead of bypassing them.

Show and hide the sidebar with the workspace request interface below. Neither action should stop development services. Do not close service panes, kill processes, or restart Rex to hide or refresh the sidebar. Installation updates must also leave existing services alone.

The workspace observer does not need model calls. Do not add a model loop or repeatedly capture screens to keep agents, project status, or service information current. Inspect only what the user's task needs.

## Discover before configuring

Project-script discovery reads existing `package.json` scripts and lockfiles, `.claude/launch.json`, `Makefile`, and `justfile`. Discovery must not execute repository commands, install dependencies, start services, or evaluate build files as a way to list commands.

When the user asks to set up a development workflow, first read the project's actual configuration, dependencies, and documentation. Check its package-manager choice and the tools available on the project host. Reuse discovered commands. Add missing editor, development/start, or build commands only when those sources establish how they should work. Do not guess that a project has `dev` or `build`, choose a package manager from habit, or claim that an editor is installed without checking.

When a coding task adds or changes a reusable development/start/build workflow, refresh discovery and register any verified missing custom commands as part of that task. Do not make the user transcribe commands already established by the repository. Discovery and saving remain separate from execution.

Commands run on the host containing the project. A remote terminal editor runs remotely. A remote GUI launcher does not open an editor on the user's local Mac. Use the Rex host guidance when the project is elsewhere, and describe the actual host rather than silently falling back to this machine.

Do not write credentials, tokens, or secret environment values into saved commands. Report missing configuration instead. Discovery or saving alone never authorizes execution. Execute only for the requested task, its necessary verification, or an explicit Run confirmation; never start a server, publish, or commit merely to populate the sidebar.

## List and save private commands

Find the absolute filesystem path of this loaded `SKILL.md` from the skill read result. Set `skill_dir` to its containing directory, not to a user-wide skill directory or a mutable deployment symlink. Resolve the worker relative to this skill's immutable runtime source:

```sh
module_dir=$(cd -- "$skill_dir/../.." && pwd -P) || exit
worker="$module_dir/worker.py"
project_dir="$PWD" # Use the actual absolute project directory on this host.
python3 "$worker" scripts --cwd "$project_dir" list
```

`list` returns JSON with `scripts` and `errors`. Read both. It needs no active OMP session. List first to avoid duplicating a discovered or saved command, and use the IDs returned by discovery instead of constructing IDs yourself.

To save a missing command, set `name` and `command` from the inspected project/tool configuration, then use:

```sh
python3 "$worker" scripts --cwd "$project_dir" add \
  --name "$name" --command "$command"
```

This saves private workspace configuration. It does not run the command or edit project manifests. Adding the same name and command is idempotent. If that name already has different content, preserve it unless the user explicitly asked to replace it; only then repeat `add` with `--replace`. Do not use replacement merely to make a setup command succeed.

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

Read the response and report failures. After a timeout, inspect existing state before retrying so a successful first request does not create duplicate terminals or services. A launch acknowledgement alone does not prove that a server is ready or a build passed. Verify only when the task calls for it, using the returned target and the installed Rex CLI guidance.

Project-script discovery is not Claude Desktop's embedded Browser, automatic verification, or full `autoPort` implementation. Do not claim those capabilities or silently add browser automation, port rewriting, or verification loops.
