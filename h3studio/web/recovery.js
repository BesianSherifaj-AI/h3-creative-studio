/* Legacy library recovery and saved cuts. All user-supplied strings go through esc. */
(() => {
  const ui = () => window.H3;
  const byId = id => document.getElementById(id);
  function pathField(id, label, kind, required = false) {
    return `<label for="${id}">${label}</label><div class="row">
      <input id="${id}" type="text" spellcheck="false" ${required ? "required" : ""} style="flex:1;min-width:0">
      <button type="button" data-pick="${id}" data-kind="${kind}">Browse</button></div>`;
  }
  function attachPickers() {
    byId("dialogBody").querySelectorAll("[data-pick]").forEach(button => {
      button.onclick = async () => {
        try {
          const value = await ui().pick(button.dataset.kind);
          if (value) byId(button.dataset.pick).value = value;
        } catch (e) { byId("dialogError").textContent = e.message; }
      };
    });
  }
  function recoveryReport(report) {
    const u = ui();
    const counts = ["compositions","revisions","shots","takes","assets"]
      .map(key => `${report[key] ?? 0} ${key}`).join(" · ");
    const notes = (report.notices || []).map(n => `<p>${u.esc(n)}</p>`).join("");
    const missing = report.missing || [];
    const rows = missing.map((item, index) => `<div class="status-card">
      <strong>${u.esc(item.name)}</strong><small>${u.esc(item.kind)}</small>
      <button type="button" data-missing="${index}">Locate file</button>
    </div>`).join("");
    u.modal("Recovery report", `<p>${counts}</p>${notes}<h3>${missing.length ? "Files to locate" : "All referenced files found"}</h3>${rows}`, [
      {label:"Saved cuts", run:history}, {label:"Done", primary:true, run:() => u.closeModal()}
    ]);
    byId("dialogBody").querySelectorAll("[data-missing]").forEach(btn => {
      btn.onclick = () => linkDialog(missing[Number(btn.dataset.missing)], report);
    });
  }
  function linkDialog(item, report) {
    const u = ui();
    u.modal("Locate missing file", `<p>${u.esc(item.name)}</p>${pathField("replacementSource","Replacement file","file",true)}
      ${item.sha256 ? `<details><summary>Expected SHA-256</summary><code>${u.esc(item.sha256)}</code></details>` : ""}`,
      [{label:"Back",run:() => recoveryReport(report)}, {label:"Relink file",primary:true,run:async () => {
        const path = byId("replacementSource").value.trim();
        if (!path) throw new Error("Select the matching file.");
        const result = await u.api("relink",{missing:item.id,path});
        await u.refresh();
        recoveryReport(result);
      }}]);
    attachPickers();
  }
  function open() {
    const u = ui();
    const state = u.get().doc;
    const missing = Object.values(state.missing || {});
    const form = `<label for="legacyKind">Source</label><select id="legacyKind">
      <option value="folder">Installed library folder</option><option value="file">Project ZIP, JSON or Atelier database</option></select>
      ${pathField("legacySource","Library or project path","folder",true)}
      ${pathField("legacyMedia","ComfyUI output folder (optional, for motion packets)","folder")}
      ${missing.length ? `<p><button type="button" id="reviewMissing">Locate ${missing.length} missing file${missing.length===1?"":"s"}</button></p>` : ""}`;
    u.modal("Import legacy project",form,[{label:"Import",primary:true,run:async () => {
      const path = byId("legacySource").value.trim();
      if (!path) throw new Error("Select a library folder or a legacy project file.");
      const entry = await u.api("recover",{path,media_root:byId("legacyMedia").value.trim() || null});
      u.modal("Importing legacy project",'<p role="status" id="recoveryWait">Copying referenced files and rebuilding history…</p>',
        [{label:"Close",run:() => u.closeModal()}]);
      const token = byId("recoveryWait");
      try {
        const result = await u.waitTask(entry.task);
        await u.refresh();
        if (token.isConnected && byId("dialog").open) recoveryReport(result);
        else u.toast("Legacy import complete.");
      } catch (e) {
        if (token.isConnected && byId("dialog").open) token.textContent = e.message;
        else u.toast(e.message);
      }
    }}]);
    byId("legacyKind").onchange = event => {
      byId("dialogBody").querySelector('[data-pick="legacySource"]').dataset.kind = event.target.value;
    };
    attachPickers();
    if (missing.length) byId("reviewMissing").onclick = () => recoveryReport({
      ...Object.fromEntries(["compositions","revisions","shots","takes","assets"].map(k=>[k,Object.keys(state[k]).length])),missing});
  }
  function cutTitle(key, documents) {
    const [group,id,branch] = key.split(":");
    if (group === "version") {
      const d = documents[id]?.document || {};
      const stamp = Number(id.split("/").pop().replace(".json",""));
      return (d.title || "Project revision") + (Number.isFinite(stamp) ? " · " + new Date(stamp*60000).toLocaleString() : "");
    }
    if (group === "export") {
      const name = documents["production_exports/"+id+"/"+branch+"/manifest.json"]?.document?.name || "Recovered film";
      return name + " · exported cut";
    }
    const legacy = {project:"projects",story:"stories",film:"films",production:"production"}[group];
    const ref = group === "production" ? legacy+"/"+id+"/record.json" : legacy+"/"+id+".json";
    const d = documents[ref]?.document || documents["portable/"+id]?.document || {};
    return (d.title || d.name || id) + (branch ? " · " + branch : "");
  }
  function history() {
    const u = ui();
    const state = u.get().doc;
    const cuts = state.legacy?.cuts || {};
    const documents = state.legacy?.documents || {};
    const saved = (state.cut_history || []).map((cut,index) => ({
      title:`Cut ${index+1}`,detail:cut.map(x=>state.shots[x.shot]?.name || "Shot").join(" · "),
      count:cut.length,op:"restore_cut",payload:{index}
    })).reverse();
    const imported = Object.entries(cuts).map(([key,cut]) => ({
      title:cutTitle(key,documents),detail:key.split(":")[0],count:cut.length,op:"legacy_cut",payload:{key}
    }));
    const rows = [...saved,...imported];
    const list = (start,items) => items.map((row,i) => `<div class="status-card"><strong>${u.esc(row.title)}</strong>
      <small>${row.count} shot${row.count === 1 ? "" : "s"} · ${u.esc(row.detail)}</small>
      <button type="button" data-cut="${i+start}" ${row.count?"":"disabled"}>Open cut</button></div>`).join("");
    const sections = (saved.length ? "<h3>Saved cuts</h3>"+list(0,saved) : "")+
      (imported.length ? "<h3>Imported takes and branches</h3>"+list(saved.length,imported) : "");
    u.modal("Cut history", sections || "<p>Previous cuts appear here after you change a take or import a library.</p>",
      [{label:"Import library",run:open},{label:"Done",primary:true,run:() => u.closeModal()}]);
    byId("dialogBody").querySelectorAll("[data-cut]").forEach(btn => {
      btn.onclick = async () => {
        try {
          const row = rows[Number(btn.dataset.cut)];
          await u.api(row.op,row.payload);
          await u.refresh();
          u.closeModal();
          u.toast("Opened "+row.title);
        } catch (e) { byId("dialogError").textContent = e.message; }
      };
    });
  }
  window.recoverTools = {open,history};
})();
