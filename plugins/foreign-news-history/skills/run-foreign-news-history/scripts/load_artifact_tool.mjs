import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

export async function loadArtifactTool() {
  const require = createRequire(import.meta.url);
  let modulePath;
  try {
    modulePath = require.resolve("@oai/artifact-tool");
  } catch (error) {
    throw new Error(
      "@oai/artifact-tool을 찾지 못했습니다. Codex 워크스페이스 의존성의 " +
        "Node.js packages 경로를 NODE_PATH로 설정해 다시 실행하세요.",
      { cause: error },
    );
  }
  return import(pathToFileURL(modulePath).href);
}
