import fs from "node:fs/promises";
import path from "node:path";
import { loadArtifactTool } from "./load_artifact_tool.mjs";

const { SpreadsheetFile, Workbook } = await loadArtifactTool();

function parseArgs(argv) {
  const args = {};
  for (let index = 2; index < argv.length; index += 1) {
    const token = argv[index];
    if (!token.startsWith("--")) continue;
    const key = token.slice(2);
    if (key === "final") {
      args.final = true;
      continue;
    }
    args[key] = argv[index + 1];
    index += 1;
  }
  return args;
}

function safeSheetName(name) {
  return name.replace(/[\\/:*?"<>|]/g, "_");
}

function applyTabularStyle(sheet, rowCount, colCount, options = {}) {
  if (!rowCount || !colCount) return;
  const used = sheet.getRangeByIndexes(0, 0, rowCount, colCount);
  used.format = {
    font: { name: "Aptos", size: 10, color: "#202124" },
    verticalAlignment: "top",
  };
  used.format.wrapText = true;
  const header = sheet.getRangeByIndexes(0, 0, 1, colCount);
  header.format = {
    fill: "#E8EAED",
    font: { name: "Aptos", size: 10, bold: true, color: "#202124" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
  };
  header.format.rowHeight = 34;
  sheet.freezePanes.freezeRows(1);
  if (options.widths) {
    options.widths.forEach((width, column) => {
      sheet.getRangeByIndexes(0, column, rowCount, 1).format.columnWidth = width;
    });
  }
  if (rowCount > 1) {
    sheet.getRangeByIndexes(1, 0, rowCount - 1, colCount).format.autofitRows();
  }
}

function addMatrixSheet(workbook, name, matrix, options = {}) {
  const sheet = workbook.worksheets.add(name);
  const normalized = matrix.length ? matrix : [["데이터 없음"]];
  const columnCount = Math.max(...normalized.map((row) => row.length));
  const rectangular = normalized.map((row) => [
    ...row,
    ...Array(Math.max(0, columnCount - row.length)).fill(null),
  ]);
  sheet.getRangeByIndexes(0, 0, rectangular.length, columnCount).values = rectangular;
  applyTabularStyle(sheet, rectangular.length, columnCount, options);
  sheet.showGridLines = false;
  return sheet;
}

async function renderAndInspect(workbook, workbookName, previewDir) {
  const qa = { workbook: workbookName, sheets: [], formulaErrors: [] };
  const overview = await workbook.inspect({
    kind: "sheet",
    include: "id,name",
    maxChars: 4000,
  });
  qa.inspect = overview.ndjson || String(overview);
  for (let index = 0; ; index += 1) {
    let sheet;
    try {
      sheet = workbook.worksheets.getItemAt(index);
    } catch {
      break;
    }
    if (!sheet) break;
    const used = sheet.getUsedRange(true);
    const values = used ? used.values : [];
    const formulas = used ? used.formulas : [];
    const rows = values?.length || 0;
    const columns = values?.[0]?.length || 0;
    for (let row = 0; row < rows; row += 1) {
      for (let column = 0; column < columns; column += 1) {
        const value = values[row]?.[column];
        if (typeof value === "string" && /^#(REF!|DIV\/0!|VALUE!|NAME\?|N\/A)$/.test(value)) {
          qa.formulaErrors.push({ sheet: sheet.name, row: row + 1, column: column + 1, value });
        }
      }
    }
    const rendered = await workbook.render({
      sheetName: sheet.name,
      autoCrop: "all",
      scale: 1,
      format: "png",
    });
    const previewPath = path.join(
      previewDir,
      `${safeSheetName(path.parse(workbookName).name)}-${safeSheetName(sheet.name)}.png`,
    );
    await fs.writeFile(previewPath, new Uint8Array(await rendered.arrayBuffer()));
    qa.sheets.push({
      name: sheet.name,
      rows,
      columns,
      formulas: Array.isArray(formulas) ? formulas.flat().filter(Boolean).length : 0,
      preview: previewPath,
    });
  }
  if (qa.formulaErrors.length) {
    throw new Error(`수식 오류 ${qa.formulaErrors.length}개가 발견되었습니다.`);
  }
  return qa;
}

function buildIntermediateWorkbook(result, review, manifest) {
  const workbook = Workbook.create();
  const historyMatrix = [result.headers, ...result.rows];
  const historySheet = addMatrixSheet(workbook, "작업이력", historyMatrix, {
    widths: [14, 13, 12, 13, 13, 13, 13, 18, 18, 10, 58, 15, 13, 13, 42],
  });
  result.rows.forEach((row, index) => {
    if (row[14]) historySheet.getCell(index + 1, 14).format.fill = "#FFF4CC";
  });

  const reviewHeaders = ["결과 행", "최종 순번", "매칭 경로", "매칭 점수", "확인 사유", ...result.headers];
  const reviewRows = review.items.map((item) => [
    item.row_number,
    item.order,
    item.origin || "",
    item.score,
    (item.reasons || []).join("; "),
    ...item.row,
  ]);
  addMatrixSheet(workbook, "확인필요", [reviewHeaders, ...reviewRows], {
    widths: [10, 10, 12, 11, 45, 14, 13, 12, 13, 13, 13, 13, 18, 18, 10, 58, 15, 13, 13, 42],
  });

  const fileHeaders = ["역할", "경로", "파일 크기", "SHA-256", "중복 제외"];
  const fileRows = (manifest.files || []).map((file) => [
    file.role,
    file.path,
    file.size,
    file.sha256,
    file.deduplicated ? "O" : "X",
  ]);
  addMatrixSheet(workbook, "입력파일", [fileHeaders, ...fileRows], {
    widths: [24, 72, 14, 66, 12],
  });

  const countRows = Object.entries(manifest.counts || {}).map(([key, value]) => [key, value]);
  const warningRows = (manifest.warnings || []).map((warning) => ["경고", warning]);
  addMatrixSheet(
    workbook,
    "실행요약",
    [
      ["항목", "값"],
      ["작업일", manifest.job_date],
      ["원본 조회일", manifest.target_source_date],
      ["생성시각", `생성 ${manifest.created_at}`],
      ...countRows,
      ...warningRows,
    ],
    { widths: [28, 80] },
  );
  return workbook;
}

function buildReviewWorkbook(result, review) {
  const workbook = Workbook.create();
  const headers = ["결과 행", "최종 순번", "매칭 경로", "매칭 점수", "확인 사유", ...result.headers];
  const rows = review.items.map((item) => [
    item.row_number,
    item.order,
    item.origin || "",
    item.score,
    (item.reasons || []).join("; "),
    ...item.row,
  ]);
  addMatrixSheet(workbook, "확인필요", [headers, ...rows], {
    widths: [10, 10, 12, 11, 45, 14, 13, 12, 13, 13, 13, 13, 18, 18, 10, 58, 15, 13, 13, 42],
  });
  return workbook;
}

const args = parseArgs(process.argv);
if (!args["output-dir"]) {
  throw new Error("Usage: build_workbooks.mjs --output-dir <job output folder> [--final]");
}

const outputDir = path.resolve(args["output-dir"]);
const previewDir = path.join(outputDir, "previews");
await fs.mkdir(previewDir, { recursive: true });
const readJson = async (name) => JSON.parse(await fs.readFile(path.join(outputDir, name), "utf8"));
const result = await readJson("result.json");
const review = await readJson("review.json");
const manifest = await readJson("manifest.json");
const checkpoint = await readJson("checkpoint.json");

if (result.rows.length !== manifest.counts.result_rows) {
  throw new Error("manifest와 result의 행 수가 다릅니다.");
}
if (result.rows.some((row) => row.length !== 15)) {
  throw new Error("A:O 15열이 아닌 결과 행이 있습니다.");
}

const qaReports = [];
if (args.final) {
  if (Number(review.count) !== 0 || Number(checkpoint.review_rows) !== 0) {
    throw new Error("확인 필요 행을 모두 해소한 뒤 작업이력_최종.xlsx를 생성해야 합니다.");
  }
  const finalWorkbook = buildIntermediateWorkbook(result, review, manifest);
  const finalName = "작업이력_최종.xlsx";
  const exported = await SpreadsheetFile.exportXlsx(finalWorkbook);
  await exported.save(path.join(outputDir, finalName));
  qaReports.push(await renderAndInspect(finalWorkbook, finalName, previewDir));
  checkpoint.phase = "excel_finalized";
  checkpoint.excel_finalized = true;
  checkpoint.final_backup = finalName;
  checkpoint.final_files = [finalName];
} else {
  const intermediateWorkbook = buildIntermediateWorkbook(result, review, manifest);
  const intermediateName = "작업이력_중간저장.xlsx";
  const intermediateExport = await SpreadsheetFile.exportXlsx(intermediateWorkbook);
  await intermediateExport.save(path.join(outputDir, intermediateName));
  qaReports.push(await renderAndInspect(intermediateWorkbook, intermediateName, previewDir));

  const reviewWorkbook = buildReviewWorkbook(result, review);
  const reviewName = "확인필요_목록.xlsx";
  const reviewExport = await SpreadsheetFile.exportXlsx(reviewWorkbook);
  await reviewExport.save(path.join(outputDir, reviewName));
  qaReports.push(await renderAndInspect(reviewWorkbook, reviewName, previewDir));

  checkpoint.phase = "intermediate_saved";
  checkpoint.intermediate_saved = true;
  checkpoint.excel_finalized = false;
  checkpoint.intermediate_files = [intermediateName, reviewName];
}
checkpoint.updated_at = new Date().toISOString();
await fs.writeFile(path.join(outputDir, "checkpoint.json"), JSON.stringify(checkpoint, null, 2), "utf8");
await fs.writeFile(path.join(outputDir, "workbook_qa.json"), JSON.stringify(qaReports, null, 2), "utf8");

process.stdout.write(
  JSON.stringify({
    outputDir,
    phase: checkpoint.phase,
    intermediateSaved: checkpoint.intermediate_saved,
    excelFinalized: checkpoint.excel_finalized,
    workbooks: qaReports.map((report) => report.workbook),
    previews: qaReports.flatMap((report) => report.sheets.map((sheet) => sheet.preview)),
  }),
);
