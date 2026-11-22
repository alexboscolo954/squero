// Automation (slice 12.6b): what each Actuator of a Contract last did with each container,
// whether the Contract automates it, and the Operator's Revert. Reasons are the Actuator's
// text: shown as text. The page pattern (slice 21.6d): figures of the containers' states on
// top, chips to filter them, each last outcome a badge.
import { Bot, CircleOff, RotateCcw, Undo2 } from "lucide-react";
import { useEffect, useState } from "react";
import * as api from "./api";
import { cpu, memory, when } from "./format";
import { useParam } from "./params";
import { PageTools } from "./Shell";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card, Figures, Skeleton, Tile } from "./ui/card";
import { Chips } from "./ui/chips";

/** A container's Automation, one state each, in this order: off wins over its rollbacks. */
export function stateOf(h: api.Held): "off" | "stopped" | "ended" | "on" {
  if (!h.automated) return "off";
  if (h.stopped) return "stopped";
  if (h.resumes != null) return "ended";
  return "on";
}

function automation(h: api.Held): string {
  const state = stateOf(h);
  if (state === "stopped") return "stopped by a Revert, until you approve one of its Sizings";
  if (state === "ended") return `ended by 3 rollbacks, resumes ${when(h.resumes)}`;
  return state;
}

/** An outcome's badge: the two the Actuator itself posts as warnings in red, a resize in green,
 * the rest (and any outcome this console doesn't know yet) neutral. */
const WRONG = ["failed", "rolled_back"];
const tone = (outcome: string) => (WRONG.includes(outcome) ? "bad" : outcome === "applied" ? "good" : "muted");

export function AutomationPage({ contract }: { contract: string }) {
  const [data, setData] = useState<api.AutomationPage | null>(null);
  const [error, setError] = useState("");
  const [failed, setFailed] = useState("");
  const [show, setShow] = useParam("state"); // "", "on" or "not": kept in the address
  useEffect(() => {
    setData(null);
    setError("");
    api.automation(contract).then(setData, (e: Error) => setError(e.message));
  }, [contract]);
  if (error) return <p className="notice">The Hub didn't answer: {error}</p>;
  if (!data) return <Skeleton />;
  const all = data.clusters.flatMap((cl) => cl.containers);
  const count = (state: ReturnType<typeof stateOf>) => all.filter((h) => stateOf(h) === state).length;
  const shown = (h: api.Held) => !show || (show === "on") === (stateOf(h) === "on");
  const undo = (cluster: string, h: api.Held) => {
    setFailed("");
    api
      .revert(contract, { cluster, namespace: h.namespace, kind: h.kind, name: h.name, container: h.container })
      .then(setData, (e: Error) => setFailed(e.message));
  };
  return (
    <div className="flex flex-col gap-5">
      <PageTools>
        <span>
          {data.clusters.length} cluster{data.clusters.length === 1 ? "" : "s"}
        </span>
        <span>
          {all.length} container{all.length === 1 ? "" : "s"}
        </span>
      </PageTools>
      {all.length > 0 && (
        <Figures>
          <Tile icon={Bot} label="On" value={String(count("on"))} note={`of ${all.length} its Actuators reported on (90 days)`} />
          <Tile icon={CircleOff} label="Off" value={String(count("off"))} note="its Automation is off" />
          <Tile icon={Undo2} label="Stopped by a Revert" value={String(count("stopped"))} note="until you approve one of its Sizings" />
          <Tile icon={RotateCcw} label="Ended by rollbacks" value={String(count("ended"))} note="3 rollbacks; it resumes at the time shown" />
        </Figures>
      )}
      <p className="hint m-0">
        What the Actuator in each cluster last did with each container (90 days). Revert puts a
        container's pods back to their template's requests, and stops its Automation until you
        approve one of its Sizings.
      </p>
      {data.kill_switch && <p className="notice">The Kill switch is on: a Revert is refused.</p>}
      {failed && <p className="notice">Couldn't: {failed}</p>}
      {data.clusters.length === 0 && <p className="hint">No cluster of this Contract runs an Actuator.</p>}
      {all.length > 0 && (
        <Chips label="Automation" value={show} onChange={setShow} options={[["", "All"], ["on", "On"], ["not", "Not on"]] as const} />
      )}
      {data.clusters.map((cl) => (
        <Card key={cl.cluster} className="p-0">
          <h2 className="m-0 px-5 pt-4 pb-3">{cl.cluster}</h2>
          {cl.containers.length === 0 ? (
            <p className="hint px-5 pb-4">Nothing reported yet.</p>
          ) : !cl.containers.some(shown) ? (
            <p className="hint px-5 pb-4">No container here matches.</p>
          ) : (
            <div className="overflow-x-auto">
            <table className="rounded-none border-x-0 border-b-0">
              <thead>
                <tr>
                  <th>Container</th>
                  <th>Automation</th>
                  <th>Last</th>
                  <th>Requests</th>
                  <th>Why</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {cl.containers.filter(shown).map((h) => {
                  const where = `${h.namespace}/${h.kind}/${h.name}/${h.container}`;
                  return (
                    <tr key={where}>
                      <td>{where}</td>
                      <td>
                        <Badge tone={!h.automated ? "muted" : automation(h) === "on" ? "good" : "bad"}>{automation(h)}</Badge>
                      </td>
                      <td>
                        <Badge tone={tone(h.outcome)}>{h.outcome.replaceAll("_", " ")}</Badge>
                        <div className="hint whitespace-nowrap">
                          {h.pods} pods, {when(h.at)}
                        </div>
                      </td>
                      <td>
                        <span className="num">
                          {h.cpu != null && `cpu ${cpu(h.cpu)} `}
                          {h.memory != null && `memory ${memory(h.memory)}`}
                        </span>
                        {h.stepped != null && <div className="hint">Step-up: memory limit {memory(h.stepped)}</div>}
                        {h.burst != null && <div className="hint">Burst: CPU limit {cpu(h.burst)}</div>}
                      </td>
                      <td className="text">{h.reason}</td>
                      <td>
                        {h.reverting ? (
                          <span className="hint">reverting</span>
                        ) : (
                          h.revertable && (
                            <Button size="sm" variant="outline" aria-label={`Revert ${where}`} onClick={() => undo(cl.cluster, h)}>
                              Revert
                            </Button>
                          )
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            </div>
          )}
        </Card>
      ))}
    </div>
  );
}
