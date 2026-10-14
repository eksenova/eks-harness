const path = require("path");
const { getDefaultConfig } = require("expo/metro-config");

const config = getDefaultConfig(__dirname);
const sdk = path.resolve(__dirname, "../../../sdk/react-native");
const escaped = path.join(sdk, "node_modules").replace(/[/\\.]/g, "\\$&");
config.watchFolders = [sdk];
config.resolver.nodeModulesPaths = [path.resolve(__dirname, "node_modules")];
config.resolver.blockList = [new RegExp(`^${escaped}\\/.*`)];
module.exports = config;
