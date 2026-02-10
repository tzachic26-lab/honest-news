import express from "express";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const app = express();
app.use(express.json());

const serverPath = resolve(__dirname, "..", "..", "servers", "HonestNewsMCPServer.py");
const pythonCmd = process.env.PYTHON_BIN || "python";

const mcpProcess = spawn(pythonCmd, [serverPath], {
  stdio: ["pipe", "pipe", "inherit"],
  env: {
    ...process.env,
    PYTHONIOENCODING: "utf-8",
  },
});

let buffer = "";
let nextId = 1;
const pending = new Map();

function sendMessage(message) {
  mcpProcess.stdin.write(`${JSON.stringify(message)}\n`);
}

function sendRequest(method, params) {
  const id = nextId++;
  const payload = { jsonrpc: "2.0", id, method, params };
  const promise = new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
  });
  sendMessage(payload);
  return promise;
}

async function initialize() {
  await sendRequest("initialize", {
    protocolVersion: "2025-06-18",
    clientInfo: { name: "honest-news-ui-bridge", version: "0.1.0" },
    capabilities: {},
  });
  sendMessage({ jsonrpc: "2.0", method: "initialized", params: {} });
}

mcpProcess.stdout.on("data", (chunk) => {
  buffer += chunk.toString();
  const lines = buffer.split("\n");
  buffer = lines.pop() ?? "";

  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      const message = JSON.parse(line);
      if (message.id && pending.has(message.id)) {
        const { resolve } = pending.get(message.id);
        pending.delete(message.id);
        resolve(message);
      }
    } catch (error) {
      console.error("Failed to parse MCP message:", error);
    }
  }
});

mcpProcess.on("exit", (code) => {
  console.error(`MCP process exited with code ${code}`);
  for (const { reject } of pending.values()) {
    reject(new Error("MCP process exited"));
  }
  pending.clear();
});

await initialize();

app.get("/api/health", (_req, res) => {
  res.json({ status: "ok" });
});

app.post("/api/call", async (req, res) => {
  const { name, arguments: args } = req.body ?? {};
  if (!name) {
    res.status(400).json({ error: "Missing tool name" });
    return;
  }
  try {
    const response = await sendRequest("tools/call", {
      name,
      arguments: args ?? {},
    });
    res.json(response.result ?? {});
  } catch (error) {
    res.status(500).json({ error: error?.message ?? "Unknown error" });
  }
});

const port = Number.parseInt(process.env.PORT || "8787", 10);
app.listen(port, () => {
  console.log(`MCP bridge listening on http://127.0.0.1:${port}`);
});
