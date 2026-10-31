"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { RequirePermission } from "@/components/RequirePermission";
import { Button, Card, ErrorText, Input, Notice, PageHeader, Select, Spinner, Table } from "@/components/ui";
import { api, unwrap, type Schemas } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { useI18n } from "@/lib/i18n";
import { key } from "@/lib/labels";

type Row = Schemas["WorkingHoursIn"];

const hhmm = (value: string) => value.slice(0, 5);

function Hours({ branchId }: { branchId: string }) {
  const { t } = useI18n();
  const { tenantId } = useAuth();
  const queryClient = useQueryClient();
  const queryKey = ["working-hours", tenantId, branchId];
  const hours = useQuery({
    queryKey,
    queryFn: () => unwrap(api.GET("/v1/branches/{branch_id}/working-hours", { params: { path: { branch_id: branchId } } })),
  });
  const [rows, setRows] = useState<Row[]>([]);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    // Editing starts from the saved hours whenever they (re)load.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (hours.data) setRows(hours.data.map(({ weekday, opens_at, closes_at }) => ({ weekday, opens_at: hhmm(opens_at), closes_at: hhmm(closes_at) })));
  }, [hours.data]);

  const save = useMutation({
    mutationFn: () =>
      unwrap(api.PUT("/v1/branches/{branch_id}/working-hours", { params: { path: { branch_id: branchId } }, body: rows })),
    onSuccess: () => {
      setSaved(true);
      void queryClient.invalidateQueries({ queryKey });
    },
  });

  if (hours.isLoading) return <Spinner />;
  if (hours.error) return <ErrorText error={hours.error} />;

  const update = (index: number, change: Partial<Row>) => {
    setSaved(false);
    setRows((current) => current.map((row, i) => (i === index ? { ...row, ...change } : row)));
  };

  return (
    <Card className="p-4">
      <p className="mb-3 text-sm text-slate-600">{t("hours.help")}</p>
      <Table>
        <thead>
          <tr>
            <th>{t("common.date")}</th>
            <th>{t("hours.opens")}</th>
            <th>{t("hours.closes")}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              <td>
                <Select aria-label={t("common.date")} value={row.weekday} onChange={(e) => update(i, { weekday: Number(e.target.value) })}>
                  {[0, 1, 2, 3, 4, 5, 6].map((d) => (
                    <option key={d} value={d}>
                      {t(key.weekday(d))}
                    </option>
                  ))}
                </Select>
              </td>
              <td>
                <Input type="time" aria-label={t("hours.opens")} value={row.opens_at} onChange={(e) => update(i, { opens_at: e.target.value })} />
              </td>
              <td>
                <Input type="time" aria-label={t("hours.closes")} value={row.closes_at} onChange={(e) => update(i, { closes_at: e.target.value })} />
                {row.closes_at <= row.opens_at && <p className="mt-1 text-xs text-slate-500">{t("hours.overnight")}</p>}
              </td>
              <td>
                <Button size="sm" variant="ghost" onClick={() => setRows((current) => current.filter((_, j) => j !== i))}>
                  {t("common.delete")}
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </Table>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button onClick={() => setRows((current) => [...current, { weekday: 0, opens_at: "09:00", closes_at: "23:00" }])}>
          + {t("hours.addRow")}
        </Button>
        <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
          {t("common.save")}
        </Button>
        {saved && <span className="text-sm text-emerald-700">{t("common.saved")}</span>}
      </div>
      <ErrorText error={save.error} />
    </Card>
  );
}

export default function HoursPage() {
  const { t } = useI18n();
  const { branchId } = useBranches();
  return (
    <RequirePermission anyOf={["branch_edit"]}>
      <PageHeader title={t("hours.title")} />
      {branchId ? <Hours key={branchId} branchId={branchId} /> : <Notice>{t("features.chooseBranch")}</Notice>}
    </RequirePermission>
  );
}
