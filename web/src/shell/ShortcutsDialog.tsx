import { Dialog } from "../components/Dialog";
import { isMac } from "../lib/hooks";

export function ShortcutsDialog({ onClose }: { onClose: () => void }) {
  const mod = isMac() ? "Cmd" : "Ctrl";
  const groups: { title: string; rows: [string[], string][] }[] = [
    {
      title: "Global",
      rows: [
        [["/"], "Focus search"],
        [["?"], "Show keyboard shortcuts"],
        [["g", "n"], "Go to Now"],
        [["g", "e"], "Go to Sessions"],
        [["g", "p"], "Go to Projects"],
        [["g", "m"], "Go to Machines"],
        [["g", "d"], "Go to Devices"],
        [["g", "b"], "Go to Browsers"],
        [["g", "k"], "Go to Backends"],
        [["g", "s"], "Go to Search"],
        [["g", ","], "Go to Settings"],
        [["Escape"], "Close, clear the selection or go back"],
      ],
    },
    {
      title: "Lists, tables and grids",
      rows: [
        [["j"], "Next item"],
        [["k"], "Previous item"],
        [["Enter"], "Open"],
        [[mod, "Enter"], "Open in a new tab"],
        [["Space"], "Toggle selection"],
        [["Shift", "Space"], "Extend the selection"],
        [[mod, "A"], "Select all on the page"],
        [["s"], "Toggle seen"],
        [["p"], "Toggle pinned"],
        [["x"], "Delete"],
        [["v"], "Switch grid and list"],
        [["f"], "Focus the filter"],
        [["["], "Previous page"],
        [["]"], "Next page"],
      ],
    },
    {
      title: "Artifact page",
      rows: [
        [["Left"], "Previous artifact"],
        [["Right"], "Next artifact"],
        [["s"], "Toggle seen"],
        [["p"], "Toggle pinned"],
        [["d"], "Download"],
        [["c"], "Copy the page link"],
        [["e"], "Edit the caption"],
        [["t"], "Focus the tag input"],
        [["x"], "Delete"],
        [["0"], "Fit image"],
        [["1"], "Actual size"],
        [["+"], "Zoom in"],
        [["-"], "Zoom out"],
        [["Space"], "Play or pause video"],
        [[mod, "F"], "Find in text"],
      ],
    },
  ];
  return (
    <Dialog title="Keyboard shortcuts" onClose={onClose} wide>
      <div className="shortcuts">
        {groups.map((group) => (
          <section key={group.title} className="shortcuts-group">
            <h3 className="subhead">{group.title}</h3>
            <table className="shortcut-table">
              <tbody>
                {group.rows.map(([combo, action]) => (
                  <tr key={combo.join("+") + action}>
                    <td className="shortcut-keys">
                      {combo.map((key) => (
                        <kbd key={key} className="kbd">
                          {key}
                        </kbd>
                      ))}
                    </td>
                    <td>{action}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        ))}
      </div>
    </Dialog>
  );
}
