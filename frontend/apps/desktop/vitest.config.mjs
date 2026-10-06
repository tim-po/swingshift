// Several suites mount connect.html/guide.html in jsdom or run real child
// processes. Under the default parallelism (one worker per file, ~3-4s startup
// each on a loaded box) those exceed vitest's 5s default and time out, though
// nothing in them waits on a deadline. The per-test budget is wall-clock slack
// only; a hung test still fails, just at 30s.
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    testTimeout: 30_000,
    hookTimeout: 30_000,
  },
});
