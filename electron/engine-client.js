// EZSlicer's window talks to its own headless engine (python -m resin_slicer.engine), the same
// HTTP API jET Hangar uses, so the printers have one owner and the API is what the UI uses.
// The engine starts on first use, hidden, with a fresh secret, and exits with this app.
//
// startEngineHost() is `EZSlicer.exe --engine`: no window, just the engine, its startup line
// passed through on stdout, for jET Hangar (which sets JET_ENGINE_TOKEN).
const { spawn } = require("child_process");
const crypto = require("crypto");
const http = require("http");

const START_TIMEOUT_MS = 20000;
const RETRY_AFTER_MS = 60000;

// Bridge command -> engine route. Streams carry progress/support messages, the result last.
const ROUTES = {
  info: { method: "GET", path: "/api/v1/info" },
  profiles: { method: "GET", path: "/api/v1/profiles" },
  preview: { method: "POST", path: "/api/v1/preview", stream: true },
  slice: { method: "POST", path: "/api/v1/slice", stream: true },
  "printer-discover": { method: "POST", path: "/api/v1/printers/discover" },
  "printer-status": { method: "POST", path: "/api/v1/printers/status" },
  "printer-status-all": { method: "POST", path: "/api/v1/printers/status-all" },
  "printer-control": { method: "POST", path: "/api/v1/printers/control" },
  "printer-send": { method: "POST", path: "/api/v1/printers/send", stream: true },
  "file-material": { method: "POST", path: "/api/v1/files/material" }
};

function engineEnv({ token, stepPython }) {
  const env = { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8", JET_ENGINE_PARENT_PID: String(process.pid) };
  if (token) env.JET_ENGINE_TOKEN = token;
  if (stepPython && !env.RESIN_SLICER_STEP_PYTHON) env.RESIN_SLICER_STEP_PYTHON = stepPython;
  return env;
}

function spawnEngine({ python, cwd, env }) {
  return spawn(python, ["-m", "resin_slicer.engine", "--engine"], {
    cwd,
    env,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"]
  });
}

class EngineClient {
  // python(), stepPython(): interpreter paths; cwd: the folder holding resin_slicer
  constructor({ python, stepPython, cwd }) {
    this.python = python;
    this.stepPython = stepPython;
    this.cwd = cwd;
    this.engine = null; // { child, port, token, ready }
    this.failedAt = 0;
  }

  available() {
    return !this.failedAt || Date.now() - this.failedAt > RETRY_AFTER_MS;
  }

  start() {
    if (this.engine) return this.engine.ready;
    const token = crypto.randomBytes(24).toString("hex");
    const child = spawnEngine({ python: this.python(), cwd: this.cwd, env: engineEnv({ token, stepPython: this.stepPython() }) });
    const engine = { child, token, port: 0 };
    this.engine = engine;
    let stderr = "";
    child.stderr.on("data", (chunk) => {
      stderr = (stderr + chunk.toString()).slice(-4000);
    });
    engine.ready = new Promise((resolve, reject) => {
      let buffer = "";
      const timer = setTimeout(() => fail(new Error("the engine didn't start in time")), START_TIMEOUT_MS);
      const fail = (error) => {
        clearTimeout(timer);
        if (this.engine === engine) this.engine = null;
        this.failedAt = Date.now();
        if (child.exitCode === null) child.kill();
        reject(error);
      };
      child.stdout.on("data", (chunk) => {
        if (engine.port) return;
        buffer += chunk.toString();
        const newline = buffer.indexOf("\n");
        if (newline < 0) return;
        try {
          const hello = JSON.parse(buffer.slice(0, newline));
          engine.port = Number(hello.port);
          clearTimeout(timer);
          this.failedAt = 0;
          resolve(engine);
        } catch (error) {
          fail(new Error(`the engine said something unexpected: ${buffer.slice(0, 200)}`));
        }
      });
      child.on("error", fail);
      child.on("exit", (code) => {
        if (this.engine === engine) this.engine = null;
        if (!engine.port) fail(new Error(stderr.trim() || `the engine exited with code ${code}`));
      });
    });
    return engine.ready;
  }

  stop() {
    if (this.engine && this.engine.child.exitCode === null) this.engine.child.kill();
    this.engine = null;
  }

  // Runs one bridge command through the engine. Resolves with the result message (same shape
  // as the JSON-lines bridge), rejects with the engine's error message.
  async run(command, payload, onMessage) {
    const route = ROUTES[command];
    if (!route) throw new Error(`no engine route for ${command}`);
    const engine = await this.start();
    return request(engine, route, payload, onMessage);
  }
}

function request(engine, route, payload, onMessage) {
  return new Promise((resolve, reject) => {
    const body = route.method === "GET" ? null : Buffer.from(JSON.stringify(payload || {}), "utf8");
    const req = http.request(
      {
        host: "127.0.0.1",
        port: engine.port,
        method: route.method,
        path: route.path,
        headers: {
          Authorization: `Bearer ${engine.token}`,
          Accept: route.stream ? "application/x-ndjson" : "application/json",
          ...(body ? { "Content-Type": "application/json", "Content-Length": body.length } : {})
        }
      },
      (res) => {
        res.setEncoding("utf8");
        let text = "";
        let final = null;
        const handle = (line) => {
          if (!line.trim()) return; // keep-alive
          let message;
          try {
            message = JSON.parse(line);
          } catch {
            return;
          }
          if ((message.type === "progress" || message.type === "support") && onMessage) onMessage(message);
          else final = message;
        };
        res.on("data", (chunk) => {
          text += chunk;
          if (!route.stream || res.statusCode !== 200) return;
          const lines = text.split(/\r?\n/);
          text = lines.pop() || "";
          lines.forEach(handle);
        });
        res.on("end", () => {
          if (res.statusCode !== 200) {
            let message = `engine answered ${res.statusCode}`;
            try {
              message = JSON.parse(text).error.message || message;
            } catch {
              // not the error shape
            }
            reject(new Error(message));
            return;
          }
          if (route.stream) {
            if (text.trim()) handle(text);
          } else {
            try {
              final = JSON.parse(text);
            } catch {
              final = null;
            }
          }
          if (!final) reject(new Error("the engine returned no answer"));
          else if (final.type === "error") reject(new Error(final.message || (final.error && final.error.message) || "the engine reported an error"));
          else resolve(final);
        });
        res.on("error", reject);
      }
    );
    req.on("error", reject);
    if (body) req.write(body);
    req.end();
  });
}

// `EZSlicer.exe --engine`: run the engine with no window; exit when it exits, or when the
// process that started us exits.
function startEngineHost({ app, python, stepPython, cwd }) {
  if (!String(process.env.JET_ENGINE_TOKEN || "").trim()) {
    process.stderr.write("EZSlicer --engine: set JET_ENGINE_TOKEN to the secret callers will send.\n");
    app.exit(2);
    return;
  }
  const child = spawnEngine({ python: python(), cwd, env: engineEnv({ stepPython: stepPython() }) });
  child.stdout.pipe(process.stdout);
  child.stderr.pipe(process.stderr);
  child.on("error", (error) => {
    process.stderr.write(`EZSlicer --engine: ${error.message}\n`);
    app.exit(1);
  });
  child.on("exit", (code) => app.exit(code ?? 0));
  const parent = process.ppid;
  setInterval(() => {
    try {
      process.kill(parent, 0); // existence check only
    } catch {
      if (child.exitCode === null) child.kill();
      app.exit(0);
    }
  }, 1000);
}

module.exports = { EngineClient, startEngineHost, ROUTES };
