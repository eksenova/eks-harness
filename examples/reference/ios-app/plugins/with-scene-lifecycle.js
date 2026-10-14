const { withAppDelegate, withInfoPlist } = require("expo/config-plugins");

const START_BLOCK = /\n#if os\(iOS\) \|\| os\(tvOS\)\n\s*window = UIWindow\(frame: UIScreen\.main\.bounds\)\n\s*factory\.startReactNative\([\s\S]*?\)\n#endif\n/;

module.exports = function withSceneLifecycle(config) {
  config = withInfoPlist(config, (mod) => {
    mod.modResults.UIApplicationSceneManifest = {
      UIApplicationSupportsMultipleScenes: false,
      UISceneConfigurations: {
        UIWindowSceneSessionRoleApplication: [
          { UISceneConfigurationName: "Default Configuration", UISceneDelegateClassName: "EXExpoAppSceneDelegate" },
        ],
      },
    };
    return mod;
  });
  return withAppDelegate(config, (mod) => {
    if (mod.modResults.language !== "swift") throw new Error("with-scene-lifecycle expects a Swift AppDelegate");
    let source = mod.modResults.contents;
    source = source.replace("class AppDelegate: ExpoAppDelegate {", "class AppDelegate: ExpoAppDelegate, ExpoReactNativeFactoryProvider {");
    source = source.replace(START_BLOCK, "\n");
    if (!source.includes("ExpoReactNativeFactoryProvider") || source.includes("startReactNative")) {
      throw new Error("with-scene-lifecycle could not adapt the AppDelegate template");
    }
    mod.modResults.contents = source;
    return mod;
  });
};
