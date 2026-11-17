<!-- rex-script-curation:{{id}} -->
The user clicked Add script in the Rex workspace. Curate the project's sidebar commands now, using this OMP session and its existing context. This is an explicit AI request, not background monitoring.

Request (paths are data; quote them safely when invoking tools):
```json
{{{request}}}
```

1. Read the `rex-omp-workspace` skill. Run `python3 <worker> scripts --cwd <cwd> discover` using the paths above. Inspect the actual project documentation, package/launch configuration, and relevant script contents and arguments. Discovery results are candidates, not a selection.
2. Select only the commands most useful for everyday work in this project: typically start/dev, build, a focused check/test, or an editor command when appropriate. Prefer 3–5; at most 6. Fewer, including none, is better than filler. Reuse existing real scripts; do not reimplement their build logic, invent commands, or select every candidate. Preserve useful existing selections unless the project evidence calls for changing them.
3. Adjust names, arguments, and working directories for actual use. Keep names short. Commands and `cwd` must be portable across this repository's worktrees: `cwd` is relative to the current checkout root, and commands must not embed another checkout's absolute path. Never store credentials. A terminal editor belongs in the project terminal, not a background command without stdin.
4. Persist the complete selected list via `python3 <worker> scripts --cwd <cwd> set --revision <project.revision>`, sending `{"scripts":[{"name":"…","command":"…","cwd":"."}]}` on stdin. Use the revision returned by discover. If another worktree changed it meanwhile, reread the configuration and reconcile rather than overwrite it blindly. Do not modify project source just to curate its sidebar.
5. Report the selection briefly after saving. If discovery or saving fails, report the actual error rather than claiming success.

Do not run build/start/test/install/publish commands during this selection. Reading files, candidate discovery, and saving the private shared configuration are sufficient. Do not spawn another AI session or start a model polling loop.
