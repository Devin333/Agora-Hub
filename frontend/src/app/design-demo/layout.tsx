import { PortalAccountProvider } from "@/components/auth/portal-account-provider"

export default function DesignDemoLayout({ children }: { children: React.ReactNode }) {
  return <PortalAccountProvider>{children}</PortalAccountProvider>
}
