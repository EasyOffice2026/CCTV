"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { RequirePermission } from "@/components/RequirePermission";
import { Badge, Button, Card, ErrorText, Field, Input, Modal, Notice, PageHeader, Select, Spinner, Table } from "@/components/ui";
import { api, unwrap } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { intlLocale, useI18n } from "@/lib/i18n";
import { TENANT_ROLES, key } from "@/lib/labels";
import { formatDateTime } from "@/lib/time";

type Role = (typeof TENANT_ROLES)[number];
type Form = { id?: string; email: string; full_name: string; role: Role; branch_ids: string[] };

function Users() {
  const { t, locale } = useI18n();
  const { me, tenantId } = useAuth();
  const { branches, nameOf, timezoneOf } = useBranches();
  const queryClient = useQueryClient();
  const users = useQuery({ queryKey: ["users", tenantId], queryFn: () => unwrap(api.GET("/v1/users")) });
  const [form, setForm] = useState<Form | null>(null);
  const [invited, setInvited] = useState<string | null>(null);

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["users"] });
  const save = useMutation({
    mutationFn: (f: Form) =>
      f.id
        ? unwrap(
            api.PATCH("/v1/users/{user_id}", {
              params: { path: { user_id: f.id } },
              body: { full_name: f.full_name, role: f.role, branch_ids: f.branch_ids },
            }),
          )
        : unwrap(api.POST("/v1/users", { body: { email: f.email, full_name: f.full_name, role: f.role, branch_ids: f.branch_ids } })),
    onSuccess: (user, f) => {
      if (!f.id) setInvited(user.email);
      setForm(null);
      void refresh();
    },
  });
  const setActive = useMutation({
    mutationFn: ({ id, active }: { id: string; active: boolean }) =>
      unwrap(api.PATCH("/v1/users/{user_id}", { params: { path: { user_id: id } }, body: { is_active: active } })),
    onSuccess: () => void refresh(),
  });
  const resetMfa = useMutation({
    mutationFn: (id: string) => unwrap(api.POST("/v1/users/{user_id}/reset-mfa", { params: { path: { user_id: id } } })),
    onSuccess: () => void refresh(),
  });

  const allBranches = (role: string) => role === "owner";
  const tz = timezoneOf(null);

  return (
    <>
      <PageHeader title={t("users.title")}>
        <Button variant="primary" onClick={() => setForm({ email: "", full_name: "", role: "reviewer", branch_ids: [] })}>
          + {t("users.invite")}
        </Button>
      </PageHeader>
      {invited && <Notice tone="green">{t("users.invited", { email: invited })}</Notice>}
      <Card className="mt-3">
        {users.isLoading ? (
          <Spinner />
        ) : (
          <Table>
            <thead>
              <tr>
                <th>{t("common.name")}</th>
                <th>{t("users.role")}</th>
                <th>{t("users.branchAccess")}</th>
                <th>{t("users.mfa")}</th>
                <th>{t("users.lastLogin")}</th>
                <th>{t("common.status")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(users.data ?? []).map((u) => (
                <tr key={u.id} className={u.is_active ? "" : "opacity-50"}>
                  <td>
                    <p className="font-medium">{u.full_name}</p>
                    <p dir="ltr" className="text-start text-xs text-slate-500">
                      {u.email}
                    </p>
                  </td>
                  <td>{t(key.role(u.role))}</td>
                  <td className="text-xs">
                    {allBranches(u.role) ? t("users.allBranches") : u.branch_ids.map(nameOf).join(", ") || "—"}
                  </td>
                  <td>{u.mfa_enabled ? <Badge tone="green">✓</Badge> : <Badge>—</Badge>}</td>
                  <td dir="ltr" className="text-start text-xs">
                    {u.last_login_at ? formatDateTime(u.last_login_at, tz, intlLocale(locale)) : t("common.never")}
                  </td>
                  <td>
                    <Badge tone={u.is_active ? "green" : "slate"}>{u.is_active ? t("users.active") : t("users.inactive")}</Badge>
                  </td>
                  <td className="text-end whitespace-nowrap">
                    {u.id !== me?.id && (
                      <>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() =>
                            setForm({
                              id: u.id,
                              email: u.email,
                              full_name: u.full_name,
                              role: u.role as Role,
                              branch_ids: u.branch_ids,
                            })
                          }
                        >
                          {t("common.edit")}
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setActive.mutate({ id: u.id, active: !u.is_active })}>
                          {u.is_active ? t("users.deactivate") : t("users.activate")}
                        </Button>
                        {u.mfa_enabled && (
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => window.confirm(t("users.resetMfaConfirm", { name: u.full_name })) && resetMfa.mutate(u.id)}
                          >
                            {t("users.resetMfa")}
                          </Button>
                        )}
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
        <ErrorText error={users.error ?? setActive.error ?? resetMfa.error} />
      </Card>

      <Modal open={!!form} onClose={() => setForm(null)} title={form?.id ? t("common.edit") : t("users.invite")}>
        {form && (
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              save.mutate(form);
            }}
          >
            <Field label={t("users.email")}>
              {(id) => (
                <Input id={id} type="email" required disabled={!!form.id} value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
              )}
            </Field>
            <Field label={t("common.name")}>
              {(id) => <Input id={id} required maxLength={200} value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} />}
            </Field>
            <Field label={t("users.role")}>
              {(id) => (
                <Select id={id} value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
                  {TENANT_ROLES.map((r) => (
                    <option key={r} value={r}>
                      {t(key.role(r))}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            {!allBranches(form.role) && (
              <fieldset className="flex flex-col gap-1">
                <legend className="mb-1 text-xs font-medium text-slate-600">{t("users.branchAccess")}</legend>
                {branches.map((b) => (
                  <label key={b.id} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={form.branch_ids.includes(b.id)}
                      onChange={(e) =>
                        setForm({
                          ...form,
                          branch_ids: e.target.checked ? [...form.branch_ids, b.id] : form.branch_ids.filter((x) => x !== b.id),
                        })
                      }
                    />
                    {b.name}
                  </label>
                ))}
              </fieldset>
            )}
            <ErrorText error={save.error} />
            <Button type="submit" variant="primary" disabled={save.isPending}>
              {form.id ? t("common.save") : t("users.invite")}
            </Button>
          </form>
        )}
      </Modal>
    </>
  );
}

export default function UsersPage() {
  return (
    <RequirePermission anyOf={["tenant_admin"]}>
      <Users />
    </RequirePermission>
  );
}
