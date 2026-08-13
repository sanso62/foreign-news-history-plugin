import fs from "node:fs/promises";
import path from "node:path";
import { loadArtifactTool } from "./load_artifact_tool.mjs";

const { FileBlob, SpreadsheetFile } = await loadArtifactTool();

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i += 1) {
    const key = argv[i];
    if (!key.startsWith("--")) continue;
    args[key.slice(2)] = argv[i + 1];
    i += 1;
  }
  return args;
}

const args = parseArgs(process.argv);
if (!args.input || !args.output) {
  throw new Error("Usage: xlsx_to_json.mjs --input <xlsx> --output <json> [--sheet <name>]");
}

const inputPath = path.resolve(args.input);
const outputPath = path.resolve(args.output);
const blob = await FileBlob.load(inputPath);
const workbook = await SpreadsheetFile.importXlsx(blob);
const sheet = args.sheet
  ? workbook.worksheets.getItem(args.sheet)
  : workbook.worksheets.getItemAt(0);
const used = sheet.getUsedRange(true);
const values = used ? used.values : [];

await fs.mkdir(path.dirname(outputPath), { recursive: true });
await fs.writeFile(
  outputPath,
  JSON.stringify(
    {
      source: inputPath,
      sheet: args.sheet || null,
      values,
    },
    null,
    2,
  ),
  "utf8",
);

process.stdout.write(
  JSON.stringify({ output: outputPath, rows: values.length, columns: values[0]?.length || 0 }),
);
