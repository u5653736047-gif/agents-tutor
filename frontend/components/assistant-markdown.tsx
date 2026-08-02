"use client";

import { Component, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";

type MarkdownErrorBoundaryProps = {
  children: ReactNode;
  content: string;
};

type MarkdownErrorBoundaryState = {
  hasError: boolean;
};

export class MarkdownErrorBoundary extends Component<
  MarkdownErrorBoundaryProps,
  MarkdownErrorBoundaryState
> {
  state: MarkdownErrorBoundaryState = { hasError: false };

  static getDerivedStateFromError(): MarkdownErrorBoundaryState {
    return { hasError: true };
  }

  componentDidUpdate(previousProps: MarkdownErrorBoundaryProps) {
    if (this.state.hasError && previousProps.content !== this.props.content) {
      this.setState({ hasError: false });
    }
  }

  render() {
    if (this.state.hasError) {
      return (
        <p className="whitespace-pre-wrap" data-slot="markdown-fallback">
          {this.props.content}
        </p>
      );
    }

    return this.props.children;
  }
}

type AssistantMarkdownProps = {
  content: string;
};

export function AssistantMarkdown({ content }: AssistantMarkdownProps) {
  return (
    <MarkdownErrorBoundary content={content}>
      <ReactMarkdown
        components={{
          code({ children }) {
            return <code className="font-mono text-caption">{children}</code>;
          },
          pre({ children }) {
            return (
              <pre className="overflow-x-auto rounded-md bg-neutral-900 p-3 text-neutral-50">
                {children}
              </pre>
            );
          },
        }}
        skipHtml
      >
        {content}
      </ReactMarkdown>
    </MarkdownErrorBoundary>
  );
}
