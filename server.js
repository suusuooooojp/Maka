const { spawn, execSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const https = require('https');
const http = require('http');

let PORT = process.env.PORT || '3000';
let HOST = process.env.HOST || '0.0.0.0';

// Cloud Run 対策: HOST に 8080 などの数字が入っていた場合は 0.0.0.0 に補正
if (/^\d+$/.test(HOST)) {
  HOST = '0.0.0.0';
}

console.log(`[SchoolHub Launcher] Initializing on host ${HOST}, port ${PORT}...`);

function findPython() {
  const localCandidates = [
    path.join(__dirname, '.python', 'bin', 'python3'),
    path.join('/tmp', '.python', 'bin', 'python3'),
    path.join(process.env.HOME || '/root', '.python', 'bin', 'python3')
  ];
  for (const p of localCandidates) {
    if (fs.existsSync(p)) return p;
  }
  const candidates = ['python3', 'python'];
  for (const cmd of candidates) {
    try {
      execSync(`${cmd} --version`, { stdio: 'ignore' });
      return cmd;
    } catch (_) {}
  }
  return null;
}

function downloadFile(url, dest) {
  return new Promise((resolve, reject) => {
    const protocol = url.startsWith('https') ? https : http;
    protocol.get(url, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        return downloadFile(res.headers.location, dest).then(resolve).catch(reject);
      }
      if (res.statusCode !== 200) {
        return reject(new Error(`HTTP ${res.statusCode}`));
      }
      const file = fs.createWriteStream(dest);
      res.pipe(file);
      file.on('finish', () => file.close(resolve));
    }).on('error', reject);
  });
}

async function ensurePython() {
  let cmd = findPython();
  if (cmd) return cmd;

  console.log('[SchoolHub Launcher] Python3 not found in container. Auto-installing standalone Python 3.12...');
  const targetDir = path.join('/tmp', '.python');
  const tarPath = path.join('/tmp', 'py.tar.gz');
  const url = 'https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.12.16%2B20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz';

  try {
    fs.mkdirSync('/tmp', { recursive: true });
    await downloadFile(url, tarPath);
    execSync(`mkdir -p ${targetDir} && tar -xzf ${tarPath} -C ${targetDir}`);
    const installed = path.join(targetDir, 'python', 'bin', 'python3');
    if (fs.existsSync(installed)) {
      console.log('[SchoolHub Launcher] Standalone Python 3.12 ready!');
      return installed;
    }
  } catch (err) {
    console.warn('[SchoolHub Launcher] Download failed:', err.message);
  }
  return null;
}

(async () => {
  const pythonCmd = await ensurePython();
  if (!pythonCmd) {
    console.error('[SchoolHub Launcher] Fatal: python3 is unavailable.');
    process.exit(1);
  }

  const linejsVendor = path.join(__dirname, 'linejs_vendor.mjs');
  const linejsPkg = path.join(__dirname, 'node_modules', '@evex-dev', 'linejs', 'package.json');
  if (!fs.existsSync(linejsVendor) && !fs.existsSync(linejsPkg)) {
    try {
      console.log('[SchoolHub Launcher] Installing Node dependencies (@evex-dev/linejs)...');
      execSync('npm install --omit=dev --no-audit --no-fund', { cwd: __dirname, stdio: 'inherit' });
    } catch (err) {
      console.warn('[SchoolHub Launcher] npm install warning:', err.message);
    }
  }

  const vendorDir = path.join(__dirname, '_vendor');
  const wheelsDir = path.join(__dirname, 'wheels');
  const bootstrapScript = path.join(__dirname, 'bootstrap_wheels.py');

  if (fs.existsSync(wheelsDir) && fs.existsSync(bootstrapScript)) {
    try {
      execSync(`${pythonCmd} bootstrap_wheels.py`, { cwd: __dirname, stdio: 'inherit' });
    } catch (_) {}
  }

  const envPythonPath = fs.existsSync(vendorDir)
    ? (process.env.PYTHONPATH ? `${vendorDir}${path.delimiter}${process.env.PYTHONPATH}` : vendorDir)
    : (process.env.PYTHONPATH || '');

  console.log(`[SchoolHub Launcher] Starting uvicorn on ${HOST}:${PORT}...`);

  const child = spawn(pythonCmd, ['-m', 'uvicorn', 'app:app', '--host', HOST, '--port', String(PORT)], {
    cwd: __dirname,
    stdio: 'inherit',
    env: {
      ...process.env,
      PYTHONUNBUFFERED: '1',
      PYTHONPATH: envPythonPath,
      PORT: String(PORT),
      HOST: HOST
    }
  });

  child.on('error', (err) => {
    console.error('[SchoolHub Launcher] Failed:', err);
    process.exit(1);
  });

  child.on('close', (code) => {
    process.exit(code ?? 0);
  });

  const cleanup = () => { if (child && !child.killed) child.kill('SIGTERM'); };
  process.on('SIGINT', () => { cleanup(); process.exit(0); });
  process.on('SIGTERM', () => { cleanup(); process.exit(0); });
})();