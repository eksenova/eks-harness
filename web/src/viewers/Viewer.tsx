import { ButtonLink } from "../components/Button";
import { DefList } from "../components/Misc";
import { formatCount, formatSize } from "../lib/format";
import { localPath } from "../lib/url";
import { FrameViewer } from "./FrameViewer";
import { HarViewer } from "./HarViewer";
import { ImageViewer } from "./ImageViewer";
import { JsonViewer } from "./JsonViewer";
import { TextViewer } from "./TextViewer";
import { viewerKind, type ViewerProps } from "./types";
import { VideoViewer } from "./VideoViewer";

function OtherViewer({ artifact, downloadUrl }: ViewerProps) {
  return (
    <div className="viewer viewer-other">
      <p>No preview for this kind of file.</p>
      <div>
        <ButtonLink to={downloadUrl ?? localPath(artifact.downloadUrl)} download icon="download">
          Download
        </ButtonLink>
      </div>
      <DefList
        items={[
          { label: "MIME type", value: artifact.mime, mono: true },
          { label: "Size", value: `${formatSize(artifact.size)} (${formatCount(artifact.size)} bytes)` },
        ]}
      />
    </div>
  );
}

export function Viewer(props: ViewerProps) {
  switch (viewerKind(props.artifact)) {
    case "image":
      return <ImageViewer {...props} />;
    case "video":
      return <VideoViewer {...props} />;
    case "frame":
      return <FrameViewer {...props} />;
    case "har":
      return <HarViewer {...props} />;
    case "text":
      return <TextViewer {...props} />;
    case "json":
      return <JsonViewer {...props} />;
    default:
      return <OtherViewer {...props} />;
  }
}
