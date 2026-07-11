"use client";

import { useCallback, useEffect, useState } from "react";
import { ImageIcon, LoaderCircle, RefreshCw, Save } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  fetchImageGenerationSettings,
  updateImageGenerationSettings,
  type ImageGenerationSettings,
} from "@/lib/api";

export function ImageGenerationSettingsCard() {
  const [settings, setSettings] = useState<ImageGenerationSettings | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [loadError, setLoadError] = useState("");

  const load = useCallback(async () => {
    setIsLoading(true);
    setLoadError("");
    try {
      const data = await fetchImageGenerationSettings();
      setSettings(data.settings);
    } catch (error) {
      const message = error instanceof Error ? error.message : "加载图片生成设置失败";
      setSettings(null);
      setLoadError(message);
      toast.error(message);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    if (active) void load();
    return () => {
      active = false;
    };
  }, [load]);

  const save = async () => {
    if (!settings) return;
    setIsSaving(true);
    try {
      const data = await updateImageGenerationSettings({
        ...settings,
        task_workers: Math.max(1, Math.min(16, Number(settings.task_workers) || 2)),
      });
      setSettings(data.settings);
      toast.success("图片生成设置已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存图片生成设置失败");
    } finally {
      setIsSaving(false);
    }
  };

  if (isLoading) {
    return (
      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="flex min-h-48 items-center justify-center p-6">
          <LoaderCircle className="size-5 animate-spin text-stone-400" />
        </CardContent>
      </Card>
    );
  }

  if (!settings) {
    return (
      <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
        <CardContent className="flex min-h-48 flex-col items-center justify-center gap-3 p-6 text-center">
          <p className="text-sm text-stone-600">{loadError || "图片生成设置加载失败"}</p>
          <Button type="button" variant="outline" onClick={() => void load()} className="h-9 rounded-xl">
            <RefreshCw className="size-4" />
            重新加载
          </Button>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
      <CardContent className="space-y-6 p-6">
        <div className="flex items-center gap-3">
          <div className="flex size-10 items-center justify-center rounded-xl bg-stone-100">
            <ImageIcon className="size-5 text-stone-600" />
          </div>
          <div>
            <h2 className="text-lg font-semibold tracking-tight">图片生成</h2>
            <p className="text-sm text-stone-500">底层模型路由与任务执行并发。</p>
          </div>
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          <div className="space-y-2">
            <label className="text-sm font-medium text-stone-700">GPT-image-2 底层模型</label>
            <Input
              value={settings.base_model}
              onChange={(event) => setSettings((current) => current ? ({ ...current, base_model: event.target.value }) : current)}
              placeholder="gpt-5-5"
              className="h-10 rounded-xl border-stone-200 bg-white font-mono shadow-none"
            />
          </div>
          <div className="space-y-2">
            <label className="text-sm font-medium text-stone-700">思考后缀底层模型</label>
            <Input
              value={settings.thinking_model}
              onChange={(event) => setSettings((current) => current ? ({ ...current, thinking_model: event.target.value }) : current)}
              placeholder="gpt-5-5-thinking"
              className="h-10 rounded-xl border-stone-200 bg-white font-mono shadow-none"
            />
          </div>
          <div className="space-y-2">
            <label className="text-sm font-medium text-stone-700">后台任务并发</label>
            <Input
              type="number"
              min="1"
              max="16"
              value={settings.task_workers}
              onChange={(event) => setSettings((current) => current ? ({ ...current, task_workers: Number(event.target.value) || 1 }) : current)}
              className="h-10 rounded-xl border-stone-200 bg-white"
            />
            <p className="text-xs text-stone-500">保存后重启服务生效。</p>
          </div>
          <label className="flex items-center gap-3 self-end rounded-xl border border-stone-200 bg-white px-4 py-3 text-sm text-stone-700">
            <Checkbox
              checked={settings.fallback_enabled}
              onCheckedChange={(checked) => setSettings((current) => current ? ({ ...current, fallback_enabled: Boolean(checked) }) : current)}
            />
            Codex 套餐额度不足时回退到通用 Codex 图片模型
          </label>
        </div>

        <div className="flex justify-end">
          <Button onClick={() => void save()} disabled={isSaving} className="h-10 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800">
            {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
            保存
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
