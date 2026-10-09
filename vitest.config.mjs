import path from "node:path";

export default {
  resolve: { alias: { "react-native": path.resolve("test/stubs/react-native.ts") } },
  test: { globals: true, include: ["test/**/*.test.ts"] },
};
