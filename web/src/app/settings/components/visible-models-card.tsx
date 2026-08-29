"use client";

import { LoaderCircle, Plus, RotateCcw, Save, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { fetchModelVisibility, updateSettingsConfig } from "@/lib/api";

import { useSettingsStore } from "../store";

type ModelOption = {
  id: string;
  owned_by?: string;
};

export function VisibleModelsCard() {
  const config = useSettingsStore((state) => state.config);
  const setVisibleModels = useSettingsStore((state) => state.setVisibleModels);
  const [options, setOptions] = useState<ModelOption[]>([]);
  const [draft, setDraft] = useState<string[] | null>(null);
  const [newModel, setNewModel] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);

  const optionIds = useMemo(
    () => options.map((item) => item.id).filter(Boolean),
    [options],
  );

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    void fetchModelVisibility()
      .then((data) => {
        if (cancelled) return;
        const normalized = data.models
          .map((item) => ({ id: String(item.id || "").trim(), owned_by: item.owned_by }))
          .filter((item) => item.id);
        const known = new Set(normalized.map((item) => item.id));
        for (const modelId of data.visible_models || []) {
          if (!known.has(modelId)) {
            normalized.push({ id: modelId, owned_by: "custom" });
            known.add(modelId);
          }
        }
        setOptions(normalized);
        setDraft(data.visible_models === null ? null : [...data.visible_models]);
      })
      .catch((error) => {
        if (!cancelled) toast.error(error instanceof Error ? error.message : "加载模型列表失败");
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const selected = draft === null ? new Set(optionIds) : new Set(draft);

  const updateDraft = (modelId: string, checked: boolean) => {
    const next = new Set(draft === null ? optionIds : draft);
    if (checked) next.add(modelId);
    else next.delete(modelId);
    setDraft(Array.from(next));
  };

  const addModel = () => {
    const modelId = newModel.trim();
    if (!modelId) return;
    if (!/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/.test(modelId)) {
      toast.error("模型 ID 只能包含字母、数字、点、下划线、冒号、斜线和短横线");
      return;
    }
    const next = new Set(draft === null ? optionIds : draft);
    next.add(modelId);
    setDraft(Array.from(next));
    if (!optionIds.includes(modelId)) {
      setOptions((current) => [...current, { id: modelId, owned_by: "custom" }]);
    }
    setNewModel("");
  };

  const removeModel = (modelId: string) => {
    updateDraft(modelId, false);
    setOptions((current) => current.filter((item) => item.id !== modelId));
  };

  const restoreDefault = () => {
    setDraft(null);
    setOptions((current) => current.filter((item) => item.owned_by !== "custom"));
  };

  const save = async () => {
    if (!config) return;
    setIsSaving(true);
    try {
      const data = await updateSettingsConfig({
        ...config,
        visible_models: draft === null ? null : Array.from(new Set(draft)),
      });
      const saved = data.config.visible_models === null
        ? null
        : Array.from(new Set(data.config.visible_models || []));
      setDraft(saved);
      setVisibleModels(saved);
      toast.success(saved === null ? "已恢复默认模型列表" : "模型可见性已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存模型可见性失败");
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
      <CardContent className="space-y-4 p-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-stone-900">可见模型</h2>
            <p className="mt-1 text-xs text-stone-500">控制 `/v1/models` 返回的模型。隐藏只影响列表，不会删除账号或模型能力。</p>
          </div>
          <Button type="button" variant="outline" className="h-9 rounded-xl border-stone-200 bg-white" onClick={restoreDefault} disabled={isLoading || isSaving}>
            <RotateCcw className="size-4" />
            恢复默认
          </Button>
        </div>

        {isLoading ? (
          <div className="flex items-center justify-center py-8"><LoaderCircle className="size-5 animate-spin text-stone-400" /></div>
        ) : (
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {options.map((model) => (
              <div key={model.id} className="flex min-w-0 items-center gap-2 rounded-xl border border-stone-200 bg-white px-3 py-2">
                <Checkbox checked={selected.has(model.id)} onCheckedChange={(checked) => updateDraft(model.id, Boolean(checked))} />
                <span className="min-w-0 flex-1 truncate font-mono text-xs text-stone-700" title={model.id}>{model.id}</span>
                {model.owned_by === "custom" ? (
                  <Button type="button" variant="ghost" size="icon" className="size-7 shrink-0 text-stone-400 hover:text-rose-600" onClick={() => removeModel(model.id)} aria-label={`移除 ${model.id}`}>
                    <X className="size-3.5" />
                  </Button>
                ) : null}
              </div>
            ))}
          </div>
        )}

        <div className="flex flex-col gap-2 sm:flex-row">
          <Input value={newModel} onChange={(event) => setNewModel(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); addModel(); } }} placeholder="添加模型 ID" className="h-10 rounded-xl border-stone-200 bg-white font-mono" />
          <Button type="button" variant="outline" className="h-10 rounded-xl border-stone-200 bg-white" onClick={addModel} disabled={isLoading || isSaving}>
            <Plus className="size-4" />
            添加
          </Button>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-stone-100 pt-3">
          <span className="text-xs text-stone-500">{draft === null ? "当前使用默认列表" : `已选择 ${selected.size} 个模型`}</span>
          <Button type="button" className="h-9 rounded-xl px-4" onClick={() => void save()} disabled={isLoading || isSaving || !config}>
            {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
            保存
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
