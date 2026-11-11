import * as fs from "node:fs";
import * as path from "node:path";
import { Command, Flags } from "@oh-my-pi/pi-utils/cli";
import { setAgentDir } from "@oh-my-pi/pi-utils/dirs";
import type * as ReadonlyRuntime from "../rex/readonly/runtime";
import { absolute, privateJson, privateRoot, workspaceModule } from "../rex/readonly/security";

/** A presentation-only entry: keep all profile-sensitive imports behind run(). */
export default class RexView extends Command {
	static description = "Follow the selected Rex subagent in a native read-only detail pane";
	static flags = {
		root: Flags.string({ description: "Absolute private workspace state directory", required: true }),
		module: Flags.string({ description: "Absolute path to this OMP installation's rex-workspace resources", required: true }),
	};
	static examples = ["rex-omp rex-view --root /private/tmp/rsv-501-12345-0123456789 --module /path/to/omp/rex-workspace"];

	async run(): Promise<void> {
		const { flags } = await this.parse(RexView);
		const root = privateRoot(absolute(flags.root!));
		const module = workspaceModule(flags.module!);
		const origin = privateJson(path.join(root, "root.json"));
		if (typeof origin.agentDir !== "string") throw new Error("Source OMP profile is unavailable");
		const agentDir = fs.realpathSync(absolute(origin.agentDir));
		if (!fs.statSync(agentDir).isDirectory()) throw new Error("Source OMP profile is unavailable");
		// This process reads the source's exact agent directory, not the invoking
		// shell's named profile. No persisted profile, account or theme is changed.
		setAgentDir(agentDir);
		process.env.OMP_CONFIG_SYNC = "0";
		const runtime: typeof ReadonlyRuntime = require("../rex/readonly/runtime");
		await runtime.run({ root, module });
	}
}
