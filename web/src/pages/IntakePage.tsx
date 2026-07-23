import { ChangeEvent, DragEvent, FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { FileCsv, LinkSimple, UploadSimple, X } from "@phosphor-icons/react";
import { apiRequest } from "../lib/api";
import { formatNumber, relativeProgress, titleCase } from "../lib/format";
import { ImportRecord } from "../types";
import { Button, InlineNotice, PageHeader, ProgressBar, StatusBadge } from "../components/ui";

function normalizeImport(payload: unknown): ImportRecord {
  const object = (payload && typeof payload === "object" && "data" in payload ? (payload as { data: unknown }).data : payload) as Record<string, unknown>;
  return {
    ...(object as unknown as ImportRecord),
    id: (object?.id ?? object?.import_id ?? "") as string | number,
    status: String(object?.status ?? "processing"),
  };
}

const terminalStatuses = new Set(["completed", "complete", "failed"]);

export function IntakePage() {
  const [input, setInput] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeImport, setActiveImport] = useState<ImportRecord | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const lineCount = useMemo(
    () => input.split(/[\n,\t]+/).map((value) => value.trim()).filter(Boolean).length,
    [input],
  );

  useEffect(() => {
    if (!activeImport?.id || terminalStatuses.has(activeImport.status.toLowerCase())) return;
    const timer = window.setInterval(async () => {
      try {
        setActiveImport(normalizeImport(await apiRequest(`/imports/${activeImport.id}`)));
      } catch {
        // The submitted import remains visible; the next poll can recover.
      }
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [activeImport?.id, activeImport?.status]);

  function acceptFile(nextFile?: File) {
    if (!nextFile) return;
    const extension = nextFile.name.toLowerCase().split(".").pop();
    if (!extension || !["csv", "tsv", "txt"].includes(extension)) {
      setError("Use a CSV, TSV or plain-text file.");
      return;
    }
    setFile(nextFile);
    setError(null);
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    acceptFile(event.dataTransfer.files[0]);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (!file && !lineCount) {
      setError("Paste at least one domain or choose a file.");
      return;
    }

    setSubmitting(true);
    try {
      let payload: unknown;
      if (file) {
        const body = new FormData();
        body.append("file", file);
        if (input.trim()) body.append("domains", input);
        payload = await apiRequest("/imports", { method: "POST", body });
      } else {
        payload = await apiRequest("/imports", {
          method: "POST",
          body: JSON.stringify({ domains: input, source: "console_paste" }),
        });
      }
      setActiveImport(normalizeImport(payload));
      setInput("");
      setFile(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The import could not be queued.");
    } finally {
      setSubmitting(false);
    }
  }

  const progress = activeImport ? relativeProgress(activeImport.processed_rows, activeImport.total_rows) : 0;
  const outcomeCounts = activeImport
    ? [
        ["Accepted", activeImport.accepted],
        ["Duplicate", activeImport.duplicates],
        ["Invalid", activeImport.invalid],
        ["Suppressed", activeImport.suppressed],
        ["Contacted", activeImport.previously_contacted],
        ["Refresh eligible", activeImport.refresh_eligible],
      ]
    : [];

  return (
    <div className="space-y-9 animate-enter">
      <PageHeader
        eyebrow="Durable intake"
        title="Import any number of domains."
        description="Paste URLs or stream a CSV/TSV. LINK OS returns an import immediately, normalises every public domain and processes it in bounded chunks."
      />

      <div className="grid gap-8 xl:grid-cols-[minmax(0,1.35fr)_minmax(20rem,0.65fr)]">
        <form className="space-y-6" onSubmit={submit}>
          <div className="space-y-2">
            <label className="text-sm font-semibold text-zinc-950" htmlFor="domain-input">Domains or URLs</label>
            <textarea
              id="domain-input"
              className="field min-h-64 resize-y font-mono text-[13px] leading-6"
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder={"publisher.example\nhttps://another-site.com/contact\nnewsroom.org.au"}
              aria-describedby="domain-help"
            />
            <div className="flex items-center justify-between gap-4 text-xs text-slate-500" id="domain-help">
              <span>One per line, or comma/tab separated. All valid public TLDs are accepted.</span>
              <span className="shrink-0 font-mono">{formatNumber(lineCount)} ready</span>
            </div>
          </div>

          <div className="relative flex items-center gap-3 text-xs uppercase tracking-[0.12em] text-slate-400 before:h-px before:flex-1 before:bg-slate-200 after:h-px after:flex-1 after:bg-slate-200">or attach</div>

          <div
            className={`rounded-xl border border-dashed p-6 transition duration-200 ${dragging ? "border-emerald-500 bg-emerald-50" : "border-slate-300 bg-white hover:border-slate-400"}`}
            onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={() => setDragging(false)}
            onDrop={handleDrop}
          >
            <input
              ref={fileInput}
              className="sr-only"
              type="file"
              accept=".csv,.tsv,.txt,text/csv,text/tab-separated-values,text/plain"
              onChange={(event: ChangeEvent<HTMLInputElement>) => acceptFile(event.target.files?.[0])}
              aria-label="Upload CSV or TSV"
            />
            {file ? (
              <div className="flex items-center gap-4">
                <span className="grid size-11 shrink-0 place-items-center rounded-lg bg-emerald-100 text-emerald-800"><FileCsv size={22} weight="regular" /></span>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-semibold text-zinc-950">{file.name}</p>
                  <p className="mt-1 font-mono text-xs text-slate-500">{formatNumber(file.size)} bytes</p>
                </div>
                <Button variant="ghost" type="button" aria-label="Remove selected file" onClick={() => setFile(null)} icon={<X size={16} weight="regular" />} />
              </div>
            ) : (
              <div className="flex flex-col items-start gap-4 sm:flex-row sm:items-center">
                <span className="grid size-11 shrink-0 place-items-center rounded-lg bg-slate-100 text-slate-600"><UploadSimple size={22} weight="regular" /></span>
                <div className="flex-1">
                  <p className="text-sm font-semibold text-zinc-950">Drop a CSV, TSV or TXT file</p>
                  <p className="mt-1 text-xs leading-5 text-slate-500">Files stream into the durable import queue; there is no business-level row cap.</p>
                </div>
                <Button type="button" onClick={() => fileInput.current?.click()}>Choose file</Button>
              </div>
            )}
          </div>

          {error && <InlineNotice tone="danger" title="Import not queued">{error}</InlineNotice>}

          <div className="flex flex-col gap-3 border-t border-slate-200 pt-6 sm:flex-row sm:items-center sm:justify-between">
            <p className="max-w-[52ch] text-xs leading-5 text-slate-500">Duplicates, contacted domains and suppressions are classified before a scrape job is created.</p>
            <Button variant="primary" type="submit" disabled={submitting} icon={<LinkSimple size={17} weight="regular" />}>
              {submitting ? "Queueing import…" : "Queue import"}
            </Button>
          </div>
        </form>

        <aside>
          <h2 className="text-lg font-semibold tracking-tight text-zinc-950">Import progress</h2>
          <p className="mt-1 text-sm text-slate-600">Counts update while the file is normalised and deduplicated.</p>
          {activeImport ? (
            <div className="mt-4 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-panel" aria-live="polite">
              <div className="border-b border-slate-200 p-5">
                <div className="flex items-center justify-between gap-4">
                  <div>
                    <p className="font-mono text-[11px] uppercase tracking-[0.12em] text-slate-500">Import {activeImport.id}</p>
                    <p className="mt-1 text-sm font-semibold text-zinc-950">{activeImport.filename ?? "Pasted domains"}</p>
                  </div>
                  <StatusBadge status={activeImport.status} />
                </div>
                <div className="mt-6">
                  <ProgressBar value={progress} label={`${formatNumber(activeImport.processed_rows ?? 0)} of ${formatNumber(activeImport.total_rows ?? 0)} rows processed`} />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-px bg-slate-200">
                {outcomeCounts.map(([label, value]) => (
                  <div className="bg-white p-4" key={label}>
                    <p className="font-mono text-xl font-medium text-zinc-950">{formatNumber(value as number | undefined)}</p>
                    <p className="mt-1 text-xs text-slate-500">{label}</p>
                  </div>
                ))}
              </div>
              {activeImport.status === "failed" && (
                <div className="border-t border-rose-200 bg-rose-50 p-4 text-sm text-rose-900">The import stopped before completion. Its accepted rows remain durable.</div>
              )}
            </div>
          ) : (
            <div className="mt-4 rounded-xl border border-dashed border-slate-300 p-6">
              <p className="text-sm font-medium text-zinc-950">No import in progress</p>
              <p className="mt-1 text-sm leading-6 text-slate-600">Submit domains to see accepted, duplicate, invalid, suppression and refresh outcomes here.</p>
            </div>
          )}

          <div className="mt-6 space-y-3 border-t border-slate-200 pt-6">
            {["Normalized to one registrable domain", "Previous outreach checked before queueing", "Failed or no-email domains refresh after 90 days"].map((label) => (
              <div className="flex items-center gap-3 text-sm text-slate-600" key={label}>
                <span className="size-1.5 rounded-full bg-emerald-600" />
                {label}
              </div>
            ))}
          </div>
        </aside>
      </div>
    </div>
  );
}
