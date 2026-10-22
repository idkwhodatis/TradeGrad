import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useNavigate, useSearch } from "@tanstack/react-router";
import {
  ArrowRight,
  ChartNoAxesCombined,
  LoaderCircle,
  ShieldCheck,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ErrorState } from "@/components/common";
import { login, queryClient } from "@/lib/api";
import { safeReturnTo, saveSession } from "@/lib/session";

const schema = z.object({
  email: z.email("Enter a valid email address."),
  password: z.string().min(1, "Enter your password."),
});
type LoginForm = z.infer<typeof schema>;

export function LoginPage() {
  const navigate = useNavigate();
  const search = useSearch({ strict: false }) as {
    next?: string;
    expired?: boolean;
  };
  const [error, setError] = useState<unknown>(null);
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<LoginForm>({ resolver: zodResolver(schema) });
  const submit = handleSubmit(async (values) => {
    setError(null);
    try {
      const token = await login(values.email, values.password);
      queryClient.clear();
      saveSession(token.access_token, token.expires_in);
      await navigate({ to: safeReturnTo(search.next), replace: true });
    } catch (failure) {
      setError(failure);
    }
  });
  return (
    <main className="login">
      <section className="login-story" aria-label="TradeGrad introduction">
        <div className="brand">
          <span className="brand-icon">
            <ChartNoAxesCombined size={24} />
          </span>
          TradeGrad
        </div>
        <div>
          <div
            className="eyebrow"
            style={{ color: "#accd8c", marginBottom: 18 }}
          >
            A MORE CONSIDERED APPROACH
          </div>
          <h1>
            Research with rigor.
            <br />
            Build with confidence.
          </h1>
          <p>
            A reproducible workspace for data, strategies, and the experiments
            that connect them.
          </p>
          <div className="login-art" aria-hidden="true">
            {[23, 39, 32, 52, 42, 68, 61, 85, 76, 100].map((height, i) => (
              <i key={i} style={{ height: `${height}%` }} />
            ))}
          </div>
        </div>
        <footer className="text-xs text-[#8eab7f]">
          M1 foundation · Synthetic seed evaluation
        </footer>
      </section>
      <section className="login-main">
        <div className="login-form">
          <span className="chip">
            <ShieldCheck size={12} /> Private research workspace
          </span>
          <h2 className="mt-5">Welcome back</h2>
          <p className="muted">Sign in to continue your research.</p>
          {search.expired && (
            <p role="status" className="notice mt-5">
              Your session expired. Sign in again to return to your work.
            </p>
          )}
          <form className="form" onSubmit={submit} noValidate>
            <div className="field">
              <label htmlFor="email">Email address</label>
              <Input
                id="email"
                type="email"
                autoComplete="username"
                autoFocus
                {...register("email")}
                aria-invalid={!!errors.email}
                aria-describedby={errors.email ? "email-error" : undefined}
                placeholder="you@example.com"
              />
              {errors.email && (
                <span id="email-error" className="field-error">
                  {errors.email.message}
                </span>
              )}
            </div>
            <div className="field">
              <label htmlFor="password">Password</label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                {...register("password")}
                aria-invalid={!!errors.password}
                aria-describedby={
                  errors.password ? "password-error" : undefined
                }
              />
              {errors.password && (
                <span id="password-error" className="field-error">
                  {errors.password.message}
                </span>
              )}
            </div>
            {Boolean(error) && <ErrorState error={error} />}
            <Button
              type="submit"
              disabled={isSubmitting}
              className="h-11 w-full"
            >
              {isSubmitting ? (
                <>
                  <LoaderCircle className="spinning" />
                  Signing in…
                </>
              ) : (
                <>
                  Sign in
                  <ArrowRight />
                </>
              )}
            </Button>
          </form>
          <p className="login-foot">
            Accounts are provisioned by your workspace administrator. This
            foundation build does not offer public registration or password
            recovery.
          </p>
        </div>
      </section>
    </main>
  );
}
