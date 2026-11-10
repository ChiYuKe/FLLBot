'use strict';

// Fixed local ELK runtime: stdin/stdout JSON only, no browser or network.
const ELK = require('./vendor/elk/elk.bundled.js');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', async () => {
  try {
    const graph = JSON.parse(input);
    const result = await new ELK().layout(graph);
    process.stdout.write(JSON.stringify(result));
  } catch (error) {
    process.stderr.write(String(error.message || error));
    process.exitCode = 1;
  }
});
