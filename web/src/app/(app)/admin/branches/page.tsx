"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { RequirePermission } from "@/components/RequirePermission";
import { Button, Card, ErrorText, Field, Input, Modal, PageHeader, Spinner, Table } from "@/components/ui";
import { api, unwrap, type Schemas } from "@/lib/api";
import { DEFAULT_TIMEZONE, useBranchList } from "@/lib/branches";
import { useI18n } from "@/lib/i18n";

type Form = { id?: string; name: string; timezone: string; address: string };

function Branches() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const branches = useBranchList();
  const [form, setForm] = useState<Form | null>(null);

  const done = () => {
    setForm(null);
    void queryClient.invalidateQueries({ queryKey: ["branches"] });
  };
  const save = useMutation({
    mutationFn: ({ id, ...body }: Form) => {
      const payload = { ...body, address: body.address || null };
      return id
        ? unwrap(api.PATCH("/v1/branches/{branch_id}", { params: { path: { branch_id: id } }, body: payload }))
        : unwrap(api.POST("/v1/branches", { body: payload }));
    },
    onSuccess: done,
  });
  const remove = useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/v1/branches/{branch_id}", { params: { path: { branch_id: id } } })),
    onSuccess: done,
  });

  const edit = (b?: Schemas["BranchOut"]) =>
    setForm(b ? { id: b.id, name: b.name, timezone: b.timezone, address: b.address ?? "" } : { name: "", timezone: DEFAULT_TIMEZONE, address: "" });

  return (
    <>
      <PageHeader title={t("branches.title")}>
        <Button variant="primary" onClick={() => edit()}>
          + {t("branches.add")}
        </Button>
      </PageHeader>
      <Card>
        {branches.isLoading ? (
          <Spinner />
        ) : (
          <Table>
            <thead>
              <tr>
                <th>{t("common.name")}</th>
                <th>{t("branches.timezone")}</th>
                <th>{t("branches.address")}</th>
                <th>{t("branches.configVersion")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(branches.data ?? []).map((b) => (
                <tr key={b.id}>
                  <td className="font-medium">{b.name}</td>
                  <td dir="ltr" className="text-start">
                    {b.timezone}
                  </td>
                  <td>{b.address}</td>
                  <td>{b.config_version}</td>
                  <td className="text-end whitespace-nowrap">
                    <Button size="sm" variant="ghost" onClick={() => edit(b)}>
                      {t("common.edit")}
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => window.confirm(t("common.confirmDelete", { name: b.name })) && remove.mutate(b.id)}
                    >
                      {t("common.delete")}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
        <ErrorText error={branches.error ?? remove.error} />
      </Card>

      <Modal open={!!form} onClose={() => setForm(null)} title={form?.id ? t("common.edit") : t("branches.add")}>
        {form && (
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              save.mutate(form);
            }}
          >
            <Field label={t("common.name")}>
              {(id) => <Input id={id} required maxLength={200} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />}
            </Field>
            <Field label={t("branches.timezone")}>
              {(id) => <Input id={id} dir="ltr" required value={form.timezone} onChange={(e) => setForm({ ...form, timezone: e.target.value })} />}
            </Field>
            <Field label={t("branches.address")} hint={t("common.optional")}>
              {(id) => <Input id={id} maxLength={500} value={form.address} onChange={(e) => setForm({ ...form, address: e.target.value })} />}
            </Field>
            <ErrorText error={save.error} />
            <Button type="submit" variant="primary" disabled={save.isPending}>
              {t("common.save")}
            </Button>
          </form>
        )}
      </Modal>
    </>
  );
}

export default function BranchesPage() {
  return (
    <RequirePermission anyOf={["tenant_admin"]}>
      <Branches />
    </RequirePermission>
  );
}
