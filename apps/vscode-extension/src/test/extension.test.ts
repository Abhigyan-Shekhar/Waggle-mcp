import * as assert from "assert";
import cp = require("child_process");
import * as fs from "fs/promises";
import * as path from "path";
import * as sinon from "sinon";
import * as vscode from "vscode";

describe("Waggle VS Code Extension Integration Tests", () => {
  let sandbox: sinon.SinonSandbox;
  let execFileStub: sinon.SinonStub;
  let showInfoStub: sinon.SinonStub;
  let showWarnStub: sinon.SinonStub;
  let showErrStub: sinon.SinonStub;
  let showSaveStub: sinon.SinonStub;

  beforeEach(() => {
    sandbox = sinon.createSandbox();

    // Stub child_process.execFile to prevent real system CLI execution
    execFileStub = sandbox.stub(cp, "execFile");

    // Stub vscode.window dialogs to prevent blocking UI prompts
    showInfoStub = sandbox.stub(vscode.window, "showInformationMessage");
    showWarnStub = sandbox.stub(vscode.window, "showWarningMessage");
    showErrStub = sandbox.stub(vscode.window, "showErrorMessage");
    showSaveStub = sandbox.stub(vscode.window, "showSaveDialog");
  });

  afterEach(async () => {
    sandbox.restore();

    // Clean up any written .vscode/mcp.json in the test workspace
    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (workspaceFolders && workspaceFolders.length > 0) {
      const vscodeDir = path.join(workspaceFolders[0].uri.fsPath, ".vscode");
      const mcpJson = path.join(vscodeDir, "mcp.json");
      try {
        await fs.unlink(mcpJson);
      } catch {}
      try {
        await fs.rmdir(vscodeDir);
      } catch {}
    }
  });

  // Test 1: Install detection when CLI is NOT installed
  it("should set status to not-installed if waggle-mcp is missing", async () => {
    // Simulate that the CLI does not exist on PATH
    execFileStub.callsFake((cmd, args, options, callback) => {
      const cb = typeof options === "function" ? options : callback;
      const err = new Error("spawn waggle-mcp ENOENT") as any;
      err.code = "ENOENT";
      cb(err, "", "");
      return {} as cp.ChildProcess;
    });

    // Make sure maybePromptInstall doesn't block
    showInfoStub.resolves(undefined);

    // Get the extension and activate it
    const extension = vscode.extensions.getExtension("Abhigyan-Shekhar.waggle-memory");
    assert.ok(extension, "Extension should be found");
    
    // Activate to trigger status checks
    await extension.activate();

    // Reset history of stub to clear any activation-time calls
    execFileStub.resetHistory();

    // Trigger status update command
    await vscode.commands.executeCommand("waggle.showStatus");

    // Verify execFile was called with --version to check installation
    const versionCall = execFileStub.getCalls().find(call => 
      call.args[0] === "waggle-mcp" && call.args[1] && call.args[1][0] === "--version"
    );
    assert.ok(versionCall, "Should attempt to check waggle-mcp version");

    // Verify the status was set to "not-installed"
    const api = extension.exports as any;
    assert.ok(api && typeof api.getState === "function", "Extension should export an API with getState");
    assert.strictEqual(api.getState().status, "not-installed", "Extension status should be 'not-installed'");
  });

  // Test 2: Onboarding flow (enable workspace and write mcp.json)
  it("should successfully run onboarding and write .vscode/mcp.json", async () => {
    // Mock successful version check and doctor run
    execFileStub.callsFake((cmd, args, options, callback) => {
      const cb = typeof options === "function" ? options : callback;
      if (args && args[0] === "--version") {
        cb(null, "0.0.1\n", "");
      } else if (args && args[0] === "doctor") {
        cb(null, "Waggle doctor check passed.\n", "");
      } else {
        cb(null, "", "");
      }
      return {} as cp.ChildProcess;
    });

    // Onboarding prompts
    // First prompt: "Enable Waggle for this workspace?..."
    showInfoStub.onCall(0).resolves("Enable Waggle");
    // Second prompt: "Review the Waggle MCP config..."
    showInfoStub.onCall(1).resolves("Write Waggle Config");
    showInfoStub.resolves(undefined);

    // Execute the command
    await vscode.commands.executeCommand("waggle.enableWorkspace");

    // Verify .vscode/mcp.json was written to the test-workspace
    const workspaceFolders = vscode.workspace.workspaceFolders;
    assert.ok(workspaceFolders && workspaceFolders.length > 0, "Workspace folder should be open");
    const mcpJsonPath = path.join(workspaceFolders[0].uri.fsPath, ".vscode", "mcp.json");
    
    const fileExists = await fs.access(mcpJsonPath).then(() => true).catch(() => false);
    assert.strictEqual(fileExists, true, ".vscode/mcp.json file should have been created");

    // Parse and verify contents of .vscode/mcp.json
    const content = await fs.readFile(mcpJsonPath, "utf8");
    const parsed = JSON.parse(content);
    assert.ok(parsed.servers && parsed.servers.waggle, "mcp.json should contain the waggle server config");
    assert.strictEqual(parsed.servers.waggle.type, "stdio");
    assert.strictEqual(parsed.servers.waggle.command, "waggle-mcp");
    assert.deepStrictEqual(parsed.servers.waggle.args, ["serve", "--transport", "stdio"]);
    assert.ok(parsed.servers.waggle.env, "env block should exist");
    assert.strictEqual(parsed.servers.waggle.env.WAGGLE_DEFAULT_TENANT_ID, "test-workspace");
    assert.strictEqual(parsed.servers.waggle.env.WAGGLE_DB_PATH, "~/.waggle/waggle.db");

    // Verify state status is updated to connected
    const extension = vscode.extensions.getExtension("Abhigyan-Shekhar.waggle-memory");
    assert.ok(extension, "Extension should be found");
    const api = extension.exports as any;
    assert.strictEqual(api.getState().status, "connected", "Extension status should be 'connected' after onboarding");
  });

  // Test 3: Doctor invocation
  it("should run doctor successfully and show information dialog", async () => {
    // Mock doctor CLI command success
    execFileStub.callsFake((cmd, args, options, callback) => {
      const cb = typeof options === "function" ? options : callback;
      if (args && args[0] === "doctor") {
        cb(null, "Waggle doctor run successful\n", "");
      } else {
        cb(null, "", "");
      }
      return {} as cp.ChildProcess;
    });

    showInfoStub.resolves(undefined);

    const result = await vscode.commands.executeCommand("waggle.doctor");
    assert.strictEqual(result, true, "Doctor execution should return true");

    // Verify execFile was called with doctor command
    const doctorCall = execFileStub.getCalls().find(call => 
      call.args[0] === "waggle-mcp" && call.args[1] && call.args[1][0] === "doctor"
    );
    assert.ok(doctorCall, "Should have executed waggle-mcp doctor");
    
    // Verify success message was shown
    const shownMessage = showInfoStub.getCalls().some(call => 
      typeof call.args[0] === "string" && call.args[0].includes("completed successfully")
    );
    assert.ok(shownMessage, "Should show a success information message");
  });

  // Test 4: Export behavior
  it("should prompt save dialog and export memory to the selected path", async () => {
    // Mock save dialog destination path
    const workspaceFolders = vscode.workspace.workspaceFolders;
    assert.ok(workspaceFolders && workspaceFolders.length > 0);
    const mockExportPath = path.join(workspaceFolders[0].uri.fsPath, "test-export.abhi");
    const mockUri = vscode.Uri.file(mockExportPath);
    showSaveStub.resolves(mockUri);

    // Mock CLI export command success
    execFileStub.callsFake((cmd, args, options, callback) => {
      const cb = typeof options === "function" ? options : callback;
      if (args && args[0] === "export") {
        cb(null, "Waggle export successful\n", "");
      } else {
        cb(null, "", "");
      }
      return {} as cp.ChildProcess;
    });

    showInfoStub.resolves(undefined);

    await vscode.commands.executeCommand("waggle.exportMemory");

    // Verify execFile was called with export command and output path
    const exportCall = execFileStub.getCalls().find(call => 
      call.args[0] === "waggle-mcp" && 
      call.args[1] && 
      call.args[1][0] === "export" && 
      call.args[1].includes(mockExportPath)
    );
    assert.ok(exportCall, "Should have executed waggle-mcp export with the chosen file path");

    // Verify info dialog was shown
    const shownInfo = showInfoStub.getCalls().some(call => 
      typeof call.args[0] === "string" && call.args[0].includes("exported to")
    );
    assert.ok(shownInfo, "Should show success message pointing to the export file");
  });
});
