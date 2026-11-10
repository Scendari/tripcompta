"""Page « IA » : modèle à copier, brouillons, aperçu avant / après, import.

Partie de l'application web (voir compta/web.py, qui installe ces pages dans l'ordre).
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from .. import models as M
from .. import services as S
from ..web import HERE


def install(site):
    """Installe ces pages dans l'application. `site` : ce que les pages ont en commun (voir web.py)."""
    app = site.app
    back = site.back
    db = site.db
    get_s = site.get_s
    render = site.render
    trip_or_404 = site.trip_or_404

    # ------------------------------------------------------------------ IA
    def mcp_conf(request: Request) -> dict:
        import json
        import sys
        root = str(HERE.parent)
        frozen = getattr(sys, "frozen", False)
        if frozen:
            cmd, args = sys.executable, ["--mcp", "--data-dir", str(db.dir)]
        else:
            cmd, args = sys.executable, ["-m", "compta.mcp_server", "--data-dir", str(db.dir)]
        conf = {"mcpServers": {"tripcompta": {"command": cmd, "args": args,
                                                  **({} if frozen else {"cwd": root})}}}
        if not frozen:
            conf["mcpServers"]["tripcompta"]["env"] = {"PYTHONPATH": root}
        cli = "claude mcp add tripcompta -- " + " ".join(
            [f'"{cmd}"'] + [f'"{a}"' if " " in a else a for a in args])
        return dict(conf=json.dumps(conf, indent=2, ensure_ascii=False), cli=cli,
                    api=str(request.base_url) + "api/docs", api_token=app.state.session_token)

    def drafts() -> Path:
        """Dossier des brouillons IA : recalculé à chaque fois (la base peut être rouverte ailleurs, par exemple
        après le déverrouillage d'un dossier protégé)."""
        d_ = db.dir / "brouillons_ia"
        d_.mkdir(exist_ok=True)
        return d_
    INIT, UPDATE = "initialisation", "mise-a-jour"
    def draft_paths(tid: int, mode: str):
        base = drafts() / f"voyage-{tid}-{mode}"
        return base.with_suffix(".txt"), Path(str(base) + ".precedent.txt")

    def ai_mode(t: M.Trip, value) -> str:
        if value in (INIT, UPDATE):
            return value
        return UPDATE if (t.participants or t.expenses) else INIT

    def trip_state_for_ai(t: M.Trip) -> str:
        """État actuel du voyage, joint au message pour l'IA (compact, lisible)."""
        import json
        from ..ops import trip_detail
        d = trip_detail(t)
        keep = dict(
            voyage_id=t.id, nom=d["nom"], lieu=d["lieu"], debut=d["debut"], fin=d["fin"], note=d["note"],
            equipe=d["equipe"], categories=d["categories"],
            participants=[{k: v for k, v in p.items() if k in ("nom", "famille", "accompagnant", "roles", "desiste",
                                                              "sa_part", "a_verse", "a_avance", "reste", "situation")
                           and v not in (None, [], False, "")} for p in d["participants"]],
            familles=d["familles"],
            depenses=[{k: v for k, v in e.items() if k in ("libelle", "type", "a_venir", "categorie", "sous_categorie", "date", "prevu",
                                                          "reel", "rembourse", "rembourse_a", "paye_par", "pour")
                       and v not in (None, [], "", False, "depense")} for e in d["depenses"]],
            mouvements=[{k: v for k, v in m.items() if k != "id" and v not in (None, "")} for m in d["mouvements"]],
            ajustements=[{k: v for k, v in a.items() if k != "id" and v not in (None, "", False)}
                         for a in d["ajustements"]],
            desistements=d["desistements"], entre_nous=[x["nom"] for x in d["entre_nous"]],
            cout_total=d["cout_total"], deja_recu=d["deja_recu"], reste_a_recevoir=d["reste_a_recevoir"],
        )
        return json.dumps(keep, ensure_ascii=False, indent=1)

    def ai_render(request: Request, s: Session, t: M.Trip, mode: str, **ctx):
        from .. import importer
        ctx.setdefault("texte", "")
        dfile, dprev = draft_paths(t.id, mode)
        blank = importer.template() if mode == INIT else importer.update_template()
        draft = dfile.read_text(encoding="utf-8") if dfile.exists() else None
        saved = dt.datetime.fromtimestamp(dfile.stat().st_mtime) if draft is not None else None
        return render(request, "ai.html", trip=t, ia_mode=mode, INIT=INIT, UPDATE=UPDATE, modele=blank,
                      guide=importer.guide(), state=trip_state_for_ai(t), draft=draft, draft_saved=saved,
                      has_prev=dprev.exists(), cur=f"/v/{t.id}/ia", ai_notes=S.ai_notes(t),
                      **mcp_conf(request), **ctx)

    @app.get("/ia")
    def ai_global():
        return back("/", "L'IA travaille sur un voyage : ouvre-le, puis clique sur « 🤖 IA » dans son menu.")

    @app.get("/v/{tid}/ia", response_class=HTMLResponse)
    def ai_page(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        return ai_render(request, s, t, ai_mode(t, request.query_params.get("modele")))

    @app.get("/ia/guide_ia.md")
    def ai_guide():
        from .. import importer
        return Response(importer.guide(), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="guide_ia_tripcompta.md"'})

    @app.get("/ia/modele-{mode}.txt")
    def ai_template(mode: str):
        from .. import importer
        if mode not in (INIT, UPDATE):
            raise HTTPException(404, "Modèle inconnu")
        text = importer.template() if mode == INIT else importer.update_template()
        return Response(text, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="modele_{mode}.txt"'})

    @app.put("/v/{tid}/ia/brouillon/{mode}")
    @app.post("/v/{tid}/ia/brouillon/{mode}")
    async def ai_draft_save(tid: int, mode: str, request: Request, s: Session = Depends(get_s)):
        """Enregistrement à la volée du modèle rempli (dans le dossier de données, écriture atomique)."""
        trip_or_404(s, tid)
        if mode not in (INIT, UPDATE):
            raise HTTPException(404, "Modèle inconnu")
        text = (await request.body()).decode("utf-8")
        if len(text) > 2_000_000:
            raise HTTPException(413, "Texte trop long")
        dfile, _ = draft_paths(tid, mode)
        tmp = dfile.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, dfile)
        return {"enregistre": dt.datetime.now().strftime("%H:%M:%S")}

    @app.post("/v/{tid}/ia/brouillon/{mode}/vider")
    def ai_draft_reset(tid: int, mode: str, s: Session = Depends(get_s)):
        """Remet le modèle au propre ; l'ancien brouillon est gardé (un seul niveau) pour pouvoir le récupérer."""
        from .. import importer
        trip_or_404(s, tid)
        if mode not in (INIT, UPDATE):
            raise HTTPException(404, "Modèle inconnu")
        blank = importer.template() if mode == INIT else importer.update_template()
        dfile, dprev = draft_paths(tid, mode)
        if dfile.exists() and dfile.read_text(encoding="utf-8").strip() != blank.strip():
            os.replace(dfile, dprev)
        dfile.unlink(missing_ok=True)
        return {"modele": blank}

    @app.post("/v/{tid}/ia/brouillon/{mode}/recuperer")
    def ai_draft_restore(tid: int, mode: str, s: Session = Depends(get_s)):
        trip_or_404(s, tid)
        if mode not in (INIT, UPDATE):
            raise HTTPException(404, "Modèle inconnu")
        dfile, dprev = draft_paths(tid, mode)
        if not dprev.exists():
            raise HTTPException(404, "Pas de brouillon précédent")
        text = dprev.read_text(encoding="utf-8")
        if dfile.exists():   # échange : rien n'est perdu
            swap = dfile.with_suffix(".swap")
            os.replace(dfile, swap)
            os.replace(dprev, dfile)
            os.replace(swap, dprev)
        else:
            os.replace(dprev, dfile)
        return {"modele": text}

    @app.post("/v/{tid}/ia/apercu", response_class=HTMLResponse)
    async def ai_preview(tid: int, request: Request, s: Session = Depends(get_s)):
        from ..ops import Ops, OpsError
        t = trip_or_404(s, tid)
        f = await request.form()
        texte, mode = f.get("texte") or "", ai_mode(t, f.get("modele"))
        try:
            res = Ops(db).import_trip(texte, str(tid), simulation=True, verifier_id=True)
            return ai_render(request, s, t, mode, texte=texte, res=res, focus="apercu")
        except OpsError as e:
            errors = getattr(e, "errors", None) or [str(e)]
            return ai_render(request, s, t, mode, texte=texte, errors=errors, focus="apercu")

    @app.post("/v/{tid}/ia/importer")
    async def ai_import(tid: int, request: Request, s: Session = Depends(get_s)):
        from ..ops import Ops, OpsError
        t = trip_or_404(s, tid)
        f = await request.form()
        texte, mode = f.get("texte") or "", ai_mode(t, f.get("modele"))
        try:
            res = Ops(db).import_trip(texte, str(tid), verifier_id=True)
        except OpsError as e:
            errors = getattr(e, "errors", None) or [str(e)]
            return ai_render(request, s, t, mode, texte=texte, errors=errors, focus="apercu")
        n = len(res.get("a_verifier") or [])
        more = (f" {n} point{'s' if n > 1 else ''} à vérifier {'sont gardés' if n > 1 else 'est gardé'} "
                "plus bas, dans « À regarder ».") if n else ""
        return back(f"/v/{tid}", res["message"] + more)

    @app.post("/v/{tid}/ia/point-verifie")
    async def ai_note_done(tid: int, request: Request, s: Session = Depends(get_s)):
        t = trip_or_404(s, tid)
        f = await request.form()
        dest = f"/v/{tid}/ia" if f.get("retour") == "ia" else f"/v/{tid}"
        if not any(n["texte"] == str(f.get("texte") or "").strip() for n in S.ai_notes(t)):
            return back(dest, "Ce point était déjà retiré")
        db.snapshot("point de l'IA vérifié")
        S.drop_ai_note(t, f.get("texte"))
        s.commit()
        return back(dest, "Point vérifié : il est retiré de la liste")

    # utilisé par d'autres pages
    site.drafts = drafts
