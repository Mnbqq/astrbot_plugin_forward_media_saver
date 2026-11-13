const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);

let days = 30;

function formatSize(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(bytes) || 0;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${index === 0 ? value : value.toFixed(1)} ${units[index]}`;
}

function formatTime(seconds) {
  const date = new Date(Number(seconds) * 1000);
  return date.toLocaleString();
}

function kindLabel(kind) {
  return (
    { image: "图片", video: "视频", audio: "语音", file: "文件" }[kind] || kind
  );
}

function setMessage(text, error = false) {
  const node = $("message");
  node.textContent = text || "";
  node.className = error ? "message error" : "message";
}

function renderRecent(items) {
  const body = $("recent");
  body.replaceChildren();
  if (!items.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 4;
    cell.className = "empty";
    cell.textContent = "暂无记录";
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }
  for (const item of items) {
    const row = document.createElement("tr");
    const values = [
      formatTime(item.saved_at),
      kindLabel(item.kind),
      String(item.path || "").split(/[\\/]/).pop(),
      formatSize(item.size),
    ];
    for (const value of values) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.appendChild(cell);
    }
    row.title = item.path || "";
    body.appendChild(row);
  }
}

function renderKinds(kinds) {
  const container = $("kinds");
  container.replaceChildren();
  if (!kinds.length) {
    const empty = document.createElement("span");
    empty.className = "muted";
    empty.textContent = "暂无数据";
    container.appendChild(empty);
    return;
  }
  for (const item of kinds) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = `${kindLabel(item.kind)} ${item.count} 个 · ${formatSize(item.size)}`;
    container.appendChild(chip);
  }
}

async function load() {
  setMessage("");
  try {
    const [stats, paths] = await Promise.all([
      bridge.apiGet("stats", { days }),
      bridge.apiGet("paths"),
    ]);
    $("count").textContent = String(stats.count ?? 0);
    $("size").textContent = formatSize(stats.size ?? 0);
    const jobs = stats.jobs || {};
    $("jobs").textContent = Object.entries(jobs)
      .map(([key, value]) => `${key}:${value}`)
      .join(" ") || "无";
    renderKinds(stats.kinds || []);
    renderRecent(stats.recent || []);
    $("paths").textContent = paths.media ? `目录：${paths.media}` : "";
  } catch (error) {
    setMessage(`加载失败：${error.message || error}`, true);
  }
}

async function clean() {
  const value = Number($("clean-days").value);
  if (!Number.isFinite(value) || value <= 0) {
    setMessage("请输入大于 0 的保留天数", true);
    return;
  }
  if (!window.confirm(`确定清理 ${value} 天前的媒体文件吗？`)) return;
  try {
    const result = await bridge.apiPost("clean", { days: value });
    setMessage(`已清理 ${result.deleted} 个文件，释放 ${formatSize(result.freed)}`);
    await load();
  } catch (error) {
    setMessage(`清理失败：${error.message || error}`, true);
  }
}

async function main() {
  await bridge.ready();
  const context = bridge.getContext();
  document.title = context?.pageTitle || document.title;
  $("days").value = String(days);
  $("days").addEventListener("change", () => {
    days = Number($("days").value);
    load();
  });
  $("refresh").addEventListener("click", load);
  $("clean").addEventListener("click", clean);
  await load();
}

main();
