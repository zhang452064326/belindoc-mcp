#!/usr/bin/env node
/**
 * Trans MCP Server - npm wrapper
 * This package wraps the Python trans-mcp server for easy installation via npx.
 */

const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

// Find Python executable
function findPython() {
  const candidates = ['python3', 'python'];
  for (const cmd of candidates) {
    try {
      const result = require('child_process').execSync(`${cmd} --version`, { encoding: 'utf8' });
      if (result.includes('Python 3')) return cmd;
    } catch (e) {}
  }
  throw new Error('Python 3 is required but not found. Please install Python 3.10+');
}

// Check if trans-mcp is installed
function checkTransMcp() {
  try {
    require('child_process').execSync('pip show trans-mcp', { encoding: 'utf8' });
    return true;
  } catch (e) {
    return false;
  }
}

// Install trans-mcp
function installTransMcp() {
  console.error('Installing trans-mcp Python package...');
  const pip = spawn('pip', ['install', 'trans-mcp'], { stdio: 'inherit' });
  return new Promise((resolve, reject) => {
    pip.on('close', (code) => {
      if (code === 0) resolve();
      else reject(new Error(`pip install failed with code ${code}`));
    });
  });
}

// Main
async function main() {
  const python = findPython();

  // Auto-install if not present
  if (!checkTransMcp()) {
    await installTransMcp();
  }

  // Run trans-mcp
  const proc = spawn(python, ['-m', 'trans_mcp.server'], {
    stdio: 'inherit',
    env: process.env
  });

  proc.on('close', (code) => {
    process.exit(code || 0);
  });
}

main().catch((err) => {
  console.error('Error:', err.message);
  process.exit(1);
});
