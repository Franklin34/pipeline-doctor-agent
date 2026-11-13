# pipeline-doctor-agent
Pipeline Doctor is an AI agent system that reacts when a Jenkins pipeline fails. It investigates the log, finds the change in GitHub that caused the failure, looks up similar past failures, and produces a diagnosis with evidence and a confidence level. If the failure is in the code and the agent is confident, it generates a fix
