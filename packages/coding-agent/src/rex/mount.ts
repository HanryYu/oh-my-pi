import { isRetiredRexExtension, loadExtensionFromFactory } from "../extensibility/extensions/loader";
import type { LoadExtensionsResult } from "../extensibility/extensions/types";
import type { EventBus } from "../utils/event-bus";
import { rexWorkspaceBridge, rexWorkspaceEnabled } from "./bridge";

/** Native sources are never discovered from disk or inherited as bound instances. */
export const REX_BRIDGE_SOURCE = "<native:rex-workspace>";

/** Bind once to this session's bus/runtime, including callers that reuse a preload. */
export async function mountRexBridge(
	loaded: LoadExtensionsResult,
	cwd: string,
	agentDir: string,
	eventBus: EventBus,
): Promise<LoadExtensionsResult> {
	const extensions = loaded.extensions.filter(extension =>
		extension.path !== REX_BRIDGE_SOURCE &&
		!isRetiredRexExtension(extension.path) && !isRetiredRexExtension(extension.resolvedPath),
	);
	const preparedExtensions = loaded.preparedExtensions?.filter(extension =>
		extension.path !== REX_BRIDGE_SOURCE &&
		!isRetiredRexExtension(extension.path) && !isRetiredRexExtension(extension.resolvedPath),
	);
	if (rexWorkspaceEnabled()) {
		extensions.push(
			await loadExtensionFromFactory(
				api => rexWorkspaceBridge(api, agentDir),
				cwd,
				eventBus,
				loaded.runtime,
				REX_BRIDGE_SOURCE,
			),
		);
	}
	return { ...loaded, extensions, preparedExtensions };
}
