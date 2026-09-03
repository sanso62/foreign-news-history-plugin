// Synthetic input workbooks only. No human answers or production data.
import fs from "node:fs/promises";
import path from "node:path";
import { loadArtifactTool } from "../plugins/foreign-news-history/skills/run-foreign-news-history/scripts/load_artifact_tool.mjs";

const { Workbook, SpreadsheetFile } = await loadArtifactTool();
const output = process.argv[2];
await fs.mkdir(output, { recursive: true });
const headers = ["작업날짜", "작업 조", "보도일", "보도시각 (KST)", "URL (단축)", "온라인 기사 URL", "매체국가", "매체명 (원어)", "매체명 (한글)", "발신지", "언어", "기자명", "제목 (한글)", "", ""];
const row = (work, report, title) => [work, "그룹", report, "09:00", null, "https://example.test/article", "", "Example News", "", "", "", "", title, null, null];
const base = [headers, row("2029. 3. 15", "2029. 3. 14", "First story"), row("2029. 3. 14", "2029. 3. 13", "Carryover story"), row("2029. 3. 13", "2029. 3. 13", "Excluded story")];
for (const variant of ["valid", "ambiguous", "wrong_headers", "date_cells", "hyperlink_formula", "empty"] ) {
  const book = Workbook.create();
  book.worksheets.add("Cover").getRange("A1").values = [["Synthetic test input"]];
  const sheet = book.worksheets.add("Exported history");
  const values = structuredClone(base);
  if (variant === "wrong_headers") values[0][12] = "제목";
  sheet.getRange("A3:O6").values = values;
  sheet.getRange("A3:O3").format = { fill: "#17365D", font: { bold: true, color: "#FFFFFF" } };
  sheet.getRange("A3:O6").format.columnWidth = 20;
  if (variant === "ambiguous") book.worksheets.add("Other history").getRange("A1:O4").values = base;
  if (variant === "date_cells") {
    sheet.getRange("A4").values = [[new Date("2029-03-15T00:00:00Z")]];
    sheet.getRange("C4").values = [[new Date("2029-03-14T00:00:00Z")]];
    sheet.getRange("A4").setNumberFormat("yyyy-mm-dd");
    sheet.getRange("C4").setNumberFormat("yyyy-mm-dd");
  }
  if (variant === "hyperlink_formula") sheet.getRange("F4").formulas = [['=HYPERLINK("https://example.test/linked","Read article")']];
  if (variant === "empty") sheet.getRange("A4:O6").clear({ applyTo: "contents" });
  await (await SpreadsheetFile.exportXlsx(book)).save(path.join(output, `${variant}.xlsx`));
  if (variant === "valid") {
    const preview = await book.render({ sheetName: sheet.name, range: "A3:F6", scale: 1, format: "png" });
    await fs.writeFile(path.join(output, "preview.png"), new Uint8Array(await preview.arrayBuffer()));
  }
}
