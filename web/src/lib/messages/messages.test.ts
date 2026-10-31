import { describe, expect, it } from "vitest";

import { translate } from "../i18n";
import { ar } from "./ar";
import { en } from "./en";

describe("UI text", () => {
  it("has an Arabic text for every English one, with the same placeholders", () => {
    expect(Object.keys(ar).sort()).toEqual(Object.keys(en).sort());
    for (const key of Object.keys(en) as (keyof typeof en)[]) {
      const placeholders = (text: string) => (text.match(/\{\w+\}/g) ?? []).sort();
      expect(placeholders(ar[key]), key).toEqual(placeholders(en[key]));
      expect(ar[key].trim(), key).not.toBe("");
    }
  });

  it("fills placeholders", () => {
    expect(translate("en", "users.invited", { email: "a@b.c" })).toBe("Invitation sent to a@b.c.");
    expect(translate("ar", "live.paused", { minutes: 5 })).toContain("5");
  });
});
