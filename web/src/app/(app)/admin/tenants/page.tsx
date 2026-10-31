"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { RequirePermission } from "@/components/RequirePermission";
import { Badge, Button, Card, ErrorText, Field, Input, Modal, Notice, PageHeader, Spinner, Table } from "@/components/ui";
import { api, unwrap } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";

const EMPTY = { name: "", slug: "", owner_name: "", owner_email: "" };

function Tenants() {
  const { t } = useI18n();
  const { tenantId, setTenant } = useAuth();
  const queryClient = useQueryClient();
  const tenants = useQuery({ queryKey: ["platform-tenants"], queryFn: () => unwrap(api.GET("/v1/platform/tenants")) });
  const [form, setForm] = useState<typeof EMPTY | null>(null);
  const [created, setCreated] = useState(false);

  const create = useMutation({
    mutationFn: (body: typeof EMPTY) => unwrap(api.POST("/v1/platform/tenants", { body })),
    onSuccess: () => {
      setForm(null);
      setCreated(true);
      void queryClient.invalidateQueries({ queryKey: ["platform-tenants"] });
    },
  });

  return (
    <>
      <PageHeader title={t("tenants.title")}>
        <Button variant="primary" onClick={() => setForm(EMPTY)}>
          + {t("tenants.add")}
        </Button>
      </PageHeader>
      {created && <Notice tone="green">{t("tenants.created")}</Notice>}
      <Card className="mt-3">
        {tenants.isLoading ? (
          <Spinner />
        ) : (
          <Table>
            <thead>
              <tr>
                <th>{t("common.name")}</th>
                <th>{t("tenants.slug")}</th>
                <th>{t("common.status")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(tenants.data ?? []).map((tenant) => (
                <tr key={tenant.id}>
                  <td className="font-medium">{tenant.name}</td>
                  <td dir="ltr" className="text-start">
                    {tenant.slug}
                  </td>
                  <td>
                    <Badge tone={tenant.status === "active" ? "green" : "amber"}>{tenant.status}</Badge>
                  </td>
                  <td className="text-end">
                    {tenant.id === tenantId ? (
                      <Badge tone="blue">{t("tenants.current")}</Badge>
                    ) : (
                      <Button size="sm" onClick={() => setTenant(tenant.id)}>
                        {t("tenants.actAs")}
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
        <ErrorText error={tenants.error} />
      </Card>

      <Modal open={!!form} onClose={() => setForm(null)} title={t("tenants.add")}>
        {form && (
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              create.mutate(form);
            }}
          >
            <Field label={t("common.name")}>
              {(id) => <Input id={id} required maxLength={200} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />}
            </Field>
            <Field label={t("tenants.slug")}>
              {(id) => (
                <Input
                  id={id}
                  dir="ltr"
                  required
                  pattern="[a-z0-9]([a-z0-9-]*[a-z0-9])?"
                  maxLength={63}
                  value={form.slug}
                  onChange={(e) => setForm({ ...form, slug: e.target.value.toLowerCase() })}
                />
              )}
            </Field>
            <Field label={t("tenants.ownerName")}>
              {(id) => <Input id={id} required value={form.owner_name} onChange={(e) => setForm({ ...form, owner_name: e.target.value })} />}
            </Field>
            <Field label={t("tenants.ownerEmail")}>
              {(id) => (
                <Input id={id} type="email" required value={form.owner_email} onChange={(e) => setForm({ ...form, owner_email: e.target.value })} />
              )}
            </Field>
            <ErrorText error={create.error} />
            <Button type="submit" variant="primary" disabled={create.isPending}>
              {t("common.save")}
            </Button>
          </form>
        )}
      </Modal>
    </>
  );
}

export default function TenantsPage() {
  return (
    <RequirePermission anyOf={["platform_admin"]}>
      <Tenants />
    </RequirePermission>
  );
}
