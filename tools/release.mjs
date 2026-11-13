#!/usr/bin/env node
// Bump the EZSlicer version everywhere, commit, and create an annotated tag.
//
//   npm run release -- patch            0.2.0 -> 0.2.1
//   npm run release -- minor            0.2.0 -> 0.3.0
//   npm run release -- major            0.2.0 -> 1.0.0
//   npm run release -- 0.4.0            explicit version
//   npm run release -- minor --push     also push the commit and tag to origin
//   npm run release -- minor --dry-run  show what would change, touch nothing
//   --skip-tests                        skip pytest + npm run check
//
// Version lives in package.json, package-lock.json, pyproject.toml and
// resin_slicer/__init__.py (__version__); all four are updated together.
// Tags are "v<version>", annotated, with the commits since the previous tag.

import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const flags = new Set(args.filter((arg) => arg.startsWith("--")));
const bump = args.find((arg) => !arg.startsWith("--"));
const dryRun = flags.has("--dry-run");

function fail(message) {
  console.error(`release: ${message}`);
  process.exit(1);
}

function run(command, commandArgs, options = {}) {
  const output = execFileSync(command, commandArgs, { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], ...options });
  return (output ?? "").toString().trim(); // null when stdio is inherited
}

function git(...gitArgs) {
  return run("git", gitArgs);
}

function parseVersion(text) {
  const match = /^(\d+)\.(\d+)\.(\d+)$/.exec(String(text).trim());
  return match ? match.slice(1).map(Number) : null;
}

function nextVersion(current, kind) {
  const parts = parseVersion(current);
  if (!parts) fail(`current version "${current}" is not x.y.z`);
  const [major, minor, patch] = parts;
  if (kind === "major") return `${major + 1}.0.0`;
  if (kind === "minor") return `${major}.${minor + 1}.0`;
  if (kind === "patch") return `${major}.${minor}.${patch + 1}`;
  if (parseVersion(kind)) return kind;
  fail(`expected patch, minor, major or x.y.z, got "${kind ?? ""}"`);
}

function compareVersions(a, b) {
  const left = parseVersion(a);
  const right = parseVersion(b);
  for (let index = 0; index < 3; index += 1) {
    if (left[index] !== right[index]) return left[index] - right[index];
  }
  return 0;
}

const files = {
  packageJson: path.join(root, "package.json"),
  packageLock: path.join(root, "package-lock.json"),
  pyproject: path.join(root, "pyproject.toml"),
  pyInit: path.join(root, "resin_slicer", "__init__.py")
};

const pkg = JSON.parse(readFileSync(files.packageJson, "utf8"));
const current = pkg.version;
const version = nextVersion(current, bump);
const tag = `v${version}`;
if (compareVersions(version, current) <= 0) fail(`${version} is not newer than the current ${current}`);

// Preconditions: clean tree, tag unused.
const status = git("status", "--porcelain");
if (status) fail(`working tree has uncommitted changes:\n${status}`);
if (git("tag", "-l", tag)) fail(`tag ${tag} already exists`);

const previousTag = (() => {
  try {
    return git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*");
  } catch {
    return "";
  }
})();
const range = previousTag ? `${previousTag}..HEAD` : "HEAD";
const changes = git("log", "--no-merges", "--format=- %s", range) || "- (no changes)";

console.log(`EZSlicer ${current} -> ${version} (${tag})`);
console.log(previousTag ? `Changes since ${previousTag}:` : "Changes (first tag):");
console.log(changes);
if (dryRun) {
  console.log("\n--dry-run: nothing changed.");
  process.exit(0);
}

if (!flags.has("--skip-tests")) {
  console.log("\nRunning tests...");
  try {
    run("python", ["-m", "pytest", "-q"], { stdio: "inherit" });
    run(process.platform === "win32" ? "npm.cmd" : "npm", ["run", "check"], { stdio: "inherit", shell: process.platform === "win32" });
  } catch {
    fail("tests or build failed; nothing was changed");
  }
}

// Rewrite the version fields, keeping each file's line endings.
function rewrite(file, transform) {
  const before = readFileSync(file, "utf8");
  const after = transform(before);
  if (after === before) fail(`could not find the version in ${path.relative(root, file)}`);
  writeFileSync(file, after);
}

rewrite(files.packageJson, (text) => text.replace(/("version"\s*:\s*")[^"]+(")/, `$1${version}$2`));
rewrite(files.packageLock, (text) => {
  const lock = JSON.parse(text);
  lock.name = pkg.name;
  lock.version = version;
  if (lock.packages && lock.packages[""]) {
    lock.packages[""].name = pkg.name;
    lock.packages[""].version = version;
  }
  const eol = text.includes("\r\n") ? "\r\n" : "\n";
  return JSON.stringify(lock, null, 2).replace(/\n/g, eol) + eol;
});
rewrite(files.pyproject, (text) => text.replace(/^(version\s*=\s*")[^"]+(")/m, `$1${version}$2`));
rewrite(files.pyInit, (text) => text.replace(/^(__version__\s*=\s*")[^"]+(")/m, `$1${version}$2`));

// Rebuild so the packaged renderer (dist/) shows the new version.
try {
  run(process.platform === "win32" ? "npm.cmd" : "npm", ["run", "build"], { stdio: "inherit", shell: process.platform === "win32" });
} catch {
  console.warn("release: vite build failed after the bump; run `npm run build` before packaging.");
}

git("add", files.packageJson, files.packageLock, files.pyproject, files.pyInit);
git("commit", "-m", `Release ${tag}`);
git("tag", "-a", tag, "-m", `EZSlicer ${tag}\n\n${changes}`);
console.log(`\nCommitted and tagged ${tag}.`);

if (flags.has("--push")) {
  const branch = git("rev-parse", "--abbrev-ref", "HEAD");
  run("git", ["push", "origin", branch, tag], { stdio: "inherit" });
  console.log(`Pushed ${branch} and ${tag} to origin.`);
} else {
  console.log(`Push when ready:  git push origin HEAD ${tag}`);
}
