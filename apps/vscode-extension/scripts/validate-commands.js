const fs = require("fs");
const path = require("path");

/**
 * Strips single-line (//) and block (/* ... *\/) comments from TypeScript/JavaScript
 * source text using a character-by-character state machine that correctly skips
 * over string literals, so comment-like sequences inside strings are preserved.
 *
 * @param {string} source - Raw TypeScript/JavaScript source text.
 * @returns {string} Source text with all comments replaced by whitespace,
 *                   preserving newlines so line numbers stay accurate.
 */
function stripComments(source) {
  let result = "";
  let i = 0;
  const len = source.length;

  while (i < len) {
    const ch = source[i];

    // ── String literals ──────────────────────────────────────────────────────
    // Advance past the entire string so that '//' or '/*' inside strings are
    // not treated as comment starters.
    if (ch === '"' || ch === "'" || ch === "`") {
      const quote = ch;
      result += ch;
      i++;
      while (i < len) {
        const c = source[i];
        if (c === "\\") {
          // Escaped character – emit both the backslash and the next char as-is.
          result += c;
          i++;
          if (i < len) {
            result += source[i];
            i++;
          }
        } else if (c === quote) {
          result += c;
          i++;
          break;
        } else {
          result += c;
          i++;
        }
      }
      continue;
    }

    // ── Potential comment opener ──────────────────────────────────────────────
    if (ch === "/" && i + 1 < len) {
      const next = source[i + 1];

      // Single-line comment: // …
      if (next === "/") {
        // Replace every character up to (but not including) the newline with a
        // space so that column positions of subsequent tokens are preserved.
        while (i < len && source[i] !== "\n") {
          result += " ";
          i++;
        }
        continue;
      }

      // Block comment: /* … */
      if (next === "*") {
        result += "  "; // replace the '/*' opener with two spaces
        i += 2;
        while (i < len) {
          if (source[i] === "*" && i + 1 < len && source[i + 1] === "/") {
            result += "  "; // replace the '*/' closer with two spaces
            i += 2;
            break;
          }
          // Preserve newlines so line-number references remain valid.
          result += source[i] === "\n" ? "\n" : " ";
          i++;
        }
        continue;
      }
    }

    result += ch;
    i++;
  }

  return result;
}

function main() {
  const extensionDir = path.resolve(__dirname, "..");
  const packageJsonPath = path.join(extensionDir, "package.json");
  const extensionTsPath = path.join(extensionDir, "src", "extension.ts");

  console.log("Validating VS Code command contributions vs implementation...");
  console.log(`package.json: ${packageJsonPath}`);
  console.log(`extension.ts: ${extensionTsPath}`);

  if (!fs.existsSync(packageJsonPath)) {
    console.error(`Error: package.json not found at ${packageJsonPath}`);
    process.exit(1);
  }
  if (!fs.existsSync(extensionTsPath)) {
    console.error(`Error: extension.ts not found at ${extensionTsPath}`);
    process.exit(1);
  }

  // 1. Extract commands from package.json
  const packageJson = JSON.parse(fs.readFileSync(packageJsonPath, "utf8"));
  const contributes = packageJson.contributes || {};
  const contributedCommands = (contributes.commands || []).map((cmd) => cmd.command);

  console.log(`Found ${contributedCommands.length} command(s) in package.json:`);
  contributedCommands.forEach((cmd) => console.log(`  - ${cmd}`));

  // 2. Strip comments from extension.ts before scanning for registerCommand calls.
  //    This prevents false positives from commented-out code, e.g.:
  //      // vscode.commands.registerCommand("waggle.oldCommand", ...)
  const rawSource = fs.readFileSync(extensionTsPath, "utf8");
  const strippedSource = stripComments(rawSource);

  const registeredCommands = [];
  const registerCommandRegex =
    /vscode\.commands\.registerCommand\s*\(\s*['"]([^'"]+)['"]/g;
  let match;
  while ((match = registerCommandRegex.exec(strippedSource)) !== null) {
    registeredCommands.push(match[1]);
  }

  console.log(
    `Found ${registeredCommands.length} registered command(s) in src/extension.ts:`
  );
  registeredCommands.forEach((cmd) => console.log(`  - ${cmd}`));

  // 3. Validate matching
  let hasErrors = false;

  // Check 3.1: Are all contributed commands registered in extension.ts?
  const missingInCode = contributedCommands.filter(
    (cmd) => !registeredCommands.includes(cmd)
  );
  if (missingInCode.length > 0) {
    console.error(
      "\n[ERROR] The following command(s) are declared in package.json but not registered in src/extension.ts:"
    );
    missingInCode.forEach((cmd) => console.error(`  - ${cmd}`));
    hasErrors = true;
  }

  // Check 3.2: Are all registered 'waggle.' commands declared in package.json?
  const unregisteredInPackage = registeredCommands.filter((cmd) => {
    // Only check 'waggle.' prefixed commands to avoid flagging external commands.
    return cmd.startsWith("waggle.") && !contributedCommands.includes(cmd);
  });
  if (unregisteredInPackage.length > 0) {
    console.error(
      "\n[ERROR] The following command(s) are registered in src/extension.ts but not declared in package.json:"
    );
    unregisteredInPackage.forEach((cmd) => console.error(`  - ${cmd}`));
    hasErrors = true;
  }

  if (hasErrors) {
    console.error("\nValidation failed! Please fix the command drift.");
    process.exit(1);
  }

  console.log(
    "\nValidation succeeded! All command contributions are properly registered."
  );
  process.exit(0);
}

main();
