import * as DocumentPicker from "expo-document-picker";
import { Directory, File, Paths } from "expo-file-system";
import * as ImagePicker from "expo-image-picker";
import { fakeMediaFile, registerFake, resolveFake, type FakeMediaFile } from "./fakes";

export * from "./fakes";

registerFake("camera", { description: "camera captures" });
registerFake("gallery", { description: "image library picks" });
registerFake("document", { description: "document picks" });

type Resolved =
  | { kind: "idle" }
  | { kind: "cancel" }
  | { kind: "error"; error: string }
  | { kind: "success"; file: FakeMediaFile & { uri: string } };

function harnessDirectory(): Directory {
  const directory = new Directory(Paths.cache, "harness-media");
  if (!directory.exists) directory.create({ intermediates: true, idempotent: true });
  return directory;
}

async function resolveMedia(slot: string): Promise<Resolved> {
  const outcome = await resolveFake(slot);
  if (outcome.kind === "idle") return { kind: "idle" };
  if (outcome.kind === "cancel") return { kind: "cancel" };
  if (outcome.kind === "error") return { kind: "error", error: outcome.error };
  const media = await fakeMediaFile(slot, outcome.state);
  if (!media) return { kind: "error", error: "the harness media payload could not be read" };
  const file = new File(harnessDirectory(), media.fileName);
  if (file.exists) file.delete();
  file.create({ intermediates: true, overwrite: true });
  file.write(media.base64, { encoding: "base64" });
  return { kind: "success", file: { ...media, uri: file.uri } };
}

function toImagePickerResult(file: FakeMediaFile & { uri: string }, wantsBase64: boolean): ImagePicker.ImagePickerResult {
  return {
    canceled: false,
    assets: [
      {
        uri: file.uri,
        width: file.width,
        height: file.height,
        fileName: file.fileName,
        mimeType: file.mimeType,
        type: "image",
        fileSize: Math.round((file.base64.length * 3) / 4),
        base64: wantsBase64 ? file.base64 : undefined,
        assetId: null,
        duration: null,
        exif: null,
      } as unknown as ImagePicker.ImagePickerAsset,
    ],
  };
}

const CANCELED: ImagePicker.ImagePickerResult = { canceled: true, assets: null };

export async function launchCameraAsync(options?: ImagePicker.ImagePickerOptions): Promise<ImagePicker.ImagePickerResult> {
  const resolved = await resolveMedia("camera");
  if (resolved.kind === "idle") return ImagePicker.launchCameraAsync(options);
  if (resolved.kind === "cancel") return CANCELED;
  if (resolved.kind === "error") throw new Error(resolved.error);
  return toImagePickerResult(resolved.file, options?.base64 === true);
}

export async function launchImageLibraryAsync(options?: ImagePicker.ImagePickerOptions): Promise<ImagePicker.ImagePickerResult> {
  const resolved = await resolveMedia("gallery");
  if (resolved.kind === "idle") return ImagePicker.launchImageLibraryAsync(options);
  if (resolved.kind === "cancel") return CANCELED;
  if (resolved.kind === "error") throw new Error(resolved.error);
  return toImagePickerResult(resolved.file, options?.base64 === true);
}

export async function getDocumentAsync(options?: DocumentPicker.DocumentPickerOptions): Promise<DocumentPicker.DocumentPickerResult> {
  const resolved = await resolveMedia("document");
  if (resolved.kind === "idle") return DocumentPicker.getDocumentAsync(options);
  if (resolved.kind === "cancel") return { canceled: true, assets: null } as DocumentPicker.DocumentPickerResult;
  if (resolved.kind === "error") throw new Error(resolved.error);
  return {
    canceled: false,
    assets: [{ uri: resolved.file.uri, name: resolved.file.fileName, size: Math.round((resolved.file.base64.length * 3) / 4), mimeType: resolved.file.mimeType }],
  } as unknown as DocumentPicker.DocumentPickerResult;
}

export async function takePictureAsync(
  cameraRef: { takePictureAsync: (options?: any) => Promise<any> } | null,
  options?: { quality?: number; base64?: boolean; skipProcessing?: boolean },
): Promise<{ uri: string; width: number; height: number; base64?: string }> {
  const resolved = await resolveMedia("camera");
  if (resolved.kind === "idle") {
    if (!cameraRef) throw new Error("no camera reference");
    return cameraRef.takePictureAsync(options);
  }
  if (resolved.kind !== "success") throw new Error(resolved.kind === "error" ? resolved.error : "the harness camera capture was canceled");
  return { uri: resolved.file.uri, width: resolved.file.width, height: resolved.file.height, base64: options?.base64 ? resolved.file.base64 : undefined };
}
